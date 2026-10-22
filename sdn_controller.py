"""
Ryu (OpenFlow 1.3) controller for the multiplayer game network.

Three modes, chosen with the GAME_QOS environment variable:

  off      baseline: plain L2 learning switch, no prioritization.
  static   baseline: a fixed rule gives ALL UDP traffic on the game port
           priority, for anybody, always.
  dynamic  proposed: the controller reacts to what is happening.
             (a) Application events: the game server tells the controller
                 (HTTP) when a player registers or leaves. The controller
                 installs / removes per-player priority flows for exactly
                 those registered IP addresses.
             (b) Network monitoring: it polls the bottleneck port counters;
                 when the link is congested it throttles all non-game
                 traffic into a rate-limited low-priority queue, and lifts the
                 throttle when the congestion clears.

Pipeline (two tables):
  table 0  classifier  : game traffic -> set_queue(1), non-game under congestion
                         -> set_queue(2), then goto table 1
  table 1  L2 forwarding (learning switch); misses go to the controller

Queues (created on the trunk ports by test_topology.py):
  queue 0 = default, queue 1 = game (highest priority), queue 2 = throttled.

Run:  GAME_QOS=dynamic ryu-manager --ofp-tcp-listen-port 6653 sdn_controller.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import ethernet, ether_types, packet
from ryu.ofproto import ofproto_v1_3

from controller_logic import CongestionDetector, EventServer

GAME_PORT = int(os.environ.get("GAME_PORT", "9999"))
MODE = os.environ.get("GAME_QOS", "dynamic").lower()          # off | static | dynamic
EVENT_HOST = os.environ.get("EVENT_HOST", "0.0.0.0")
EVENT_PORT = int(os.environ.get("EVENT_PORT", "8080"))
TRUNK_BPS = float(os.environ.get("TRUNK_BPS", "10000000"))
POLL_SECS = float(os.environ.get("POLL_SECS", "1"))

Q_GAME, Q_THROTTLE = 1, 2
T_CLASSIFY, T_FORWARD = 0, 1
PRIO_MISS, PRIO_L2, PRIO_THROTTLE, PRIO_GAME = 0, 10, 50, 100


class GameQosController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if MODE not in ("off", "static", "dynamic"):
            raise SystemExit("GAME_QOS must be off, static or dynamic")
        self.datapaths = {}
        self.mac_to_port = {}
        self.players = {}        # ip -> name (from application events)
        self.trunk_port = {}     # dpid -> port_no of the bottleneck port
        self.detectors = {}      # dpid -> CongestionDetector
        self.congested = False
        self.logger.info("=== MODE: %s | game UDP port %d ===", MODE, GAME_PORT)

        self.api = EventServer(EVENT_HOST, EVENT_PORT, self._on_app_event, self._status)
        hub.spawn(self.api.serve_forever)
        if MODE == "dynamic":
            hub.spawn(self._monitor_loop)

    # ------------------------------------------------------------ helpers
    def _add_flow(self, dp, table, priority, match, instructions):
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, priority=priority,
                                      match=match, instructions=instructions))

    def _del_flow(self, dp, table, priority, match):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, table_id=table, priority=priority,
                                      match=match, command=ofp.OFPFC_DELETE_STRICT,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))

    def _queue_then_forward(self, dp, queue):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        return [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS,
                                             [parser.OFPActionSetQueue(queue)]),
                parser.OFPInstructionGotoTable(T_FORWARD)]

    def _goto_forward(self, dp):
        return [dp.ofproto_parser.OFPInstructionGotoTable(T_FORWARD)]

    # game-flow matches -------------------------------------------------
    def _static_matches(self, dp):
        p = dp.ofproto_parser
        base = dict(eth_type=ether_types.ETH_TYPE_IP, ip_proto=17)
        return [p.OFPMatch(udp_dst=GAME_PORT, **base), p.OFPMatch(udp_src=GAME_PORT, **base)]

    def _player_matches(self, dp, ip):
        p = dp.ofproto_parser
        base = dict(eth_type=ether_types.ETH_TYPE_IP, ip_proto=17)
        return [p.OFPMatch(ipv4_src=ip, udp_dst=GAME_PORT, **base),   # player -> server
                p.OFPMatch(ipv4_dst=ip, udp_src=GAME_PORT, **base)]   # server -> player

    def _throttle_match(self, dp):
        return dp.ofproto_parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP)

    # ---------------------------------------------------- switch lifecycle
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # table 0 miss -> go on to the forwarding table
        self._add_flow(dp, T_CLASSIFY, PRIO_MISS, parser.OFPMatch(), self._goto_forward(dp))
        # table 1 miss -> controller (MAC learning)
        to_ctrl = [parser.OFPInstructionActions(
            ofp.OFPIT_APPLY_ACTIONS,
            [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])]
        self._add_flow(dp, T_FORWARD, PRIO_MISS, parser.OFPMatch(), to_ctrl)

        if MODE == "static":
            for m in self._static_matches(dp):
                self._add_flow(dp, T_CLASSIFY, PRIO_GAME, m, self._queue_then_forward(dp, Q_GAME))
        elif MODE == "dynamic":
            for ip in self.players:
                self._install_player(dp, ip)
            if self.congested:
                self._install_throttle(dp)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            # learn which port is the bottleneck trunk (name contains "trunk")
            dp.send_msg(dp.ofproto_parser.OFPPortDescStatsRequest(dp, 0))
        else:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_reply(self, ev):
        dp = ev.msg.datapath
        for p in ev.msg.body:
            name = p.name.decode() if isinstance(p.name, bytes) else p.name
            if "trunk" in name.strip("\x00"):
                self.trunk_port[dp.id] = p.port_no
                self.detectors[dp.id] = CongestionDetector(TRUNK_BPS)
                self.logger.info("dpid %s: monitoring trunk port %s (%s)", dp.id, p.port_no,
                                 name.strip("\x00"))

    # ------------------------------------------- (a) application events
    def _install_player(self, dp, ip):
        for m in self._player_matches(dp, ip):
            self._add_flow(dp, T_CLASSIFY, PRIO_GAME, m, self._queue_then_forward(dp, Q_GAME))

    def _remove_player(self, dp, ip):
        for m in self._player_matches(dp, ip):
            self._del_flow(dp, T_CLASSIFY, PRIO_GAME, m)

    def _on_app_event(self, event, ip, name):
        """Called by the HTTP API when the game server reports an event."""
        if event == "register":
            new = ip not in self.players
            self.players[ip] = name
            self.logger.info("APP EVENT: player %s (%s) registered -> %s", name, ip,
                             "priority flows installed" if MODE == "dynamic" else "no action in this mode")
            if MODE == "dynamic" and new:
                for dp in self.datapaths.values():
                    self._install_player(dp, ip)
        else:
            if self.players.pop(ip, None) is not None:
                self.logger.info("APP EVENT: player %s (%s) left -> %s", name, ip,
                                 "priority flows removed" if MODE == "dynamic" else "no action in this mode")
                if MODE == "dynamic":
                    for dp in self.datapaths.values():
                        self._remove_player(dp, ip)
        return {"players": len(self.players)}

    def _status(self):
        return {"mode": MODE, "players": self.players, "congested": self.congested,
                "switches": sorted(self.datapaths), "trunk_ports": self.trunk_port}

    # ------------------------------------------- (b) network monitoring
    def _install_throttle(self, dp):
        self._add_flow(dp, T_CLASSIFY, PRIO_THROTTLE, self._throttle_match(dp),
                       self._queue_then_forward(dp, Q_THROTTLE))

    def _remove_throttle(self, dp):
        self._del_flow(dp, T_CLASSIFY, PRIO_THROTTLE, self._throttle_match(dp))

    def _monitor_loop(self):
        while True:
            hub.sleep(POLL_SECS)
            for dpid, port in list(self.trunk_port.items()):
                dp = self.datapaths.get(dpid)
                if dp:
                    dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, port))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        if MODE != "dynamic":      # baselines never react to congestion
            return
        dp = ev.msg.datapath
        det = self.detectors.get(dp.id)
        if not det:
            return
        for st in ev.msg.body:
            if st.port_no != self.trunk_port.get(dp.id):
                continue
            util, _, changed = det.update(st.tx_bytes, time.time())
            if changed:
                self.logger.info("MONITOR: dpid %s trunk utilization %.0f%% -> link %s",
                                 dp.id, util * 100, "CONGESTED" if det.congested else "recovered")
            self._apply_congestion(any(d.congested for d in self.detectors.values()))

    def _apply_congestion(self, congested):
        if congested == self.congested:
            return
        self.congested = congested
        self.logger.info("ACTION: %s throttling of non-game traffic (queue %d)",
                         "ENABLING" if congested else "DISABLING", Q_THROTTLE)
        for dp in self.datapaths.values():
            (self._install_throttle if congested else self._remove_throttle)(dp)

    # ------------------------------------------------ L2 learning (table 1)
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        eth = packet.Packet(msg.data).get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)

        if out_port != ofp.OFPP_FLOOD:
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            self._add_flow(dp, T_FORWARD, PRIO_L2, parser.OFPMatch(eth_dst=eth.dst), inst)

        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
            actions=[parser.OFPActionOutput(out_port)], data=msg.data))
