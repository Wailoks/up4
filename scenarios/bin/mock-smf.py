#!/usr/local/bin/python3

# SPDX-FileCopyrightText: 2020-present Open Networking Foundation <info@opennetworking.org>
#
# SPDX-License-Identifier: LicenseRef-ONF-Member-1.0

import argparse
import socket
import struct
import time
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv4Address, AddressValueError
from threading import Lock, Thread
from typing import Dict, Generator, Optional, IO

import ifcfg
from scapy import all as scapy
from scapy.contrib import pfcp
from scapy.layers.inet import IP, UDP
from scapy.utils import wrpcap

MSG_TYPES = {name: num for num, name in pfcp.PFCPmessageType.items()}
HEARTBEAT_PERIOD = 5  # in seconds
UDP_PORT_PFCP = 8805
IFACE_ACCESS = 0
IFACE_CORE = 1


@dataclass
class Session:
    our_seid: int
    peer_seid: int
    sent_pdrs: Dict[int, pfcp.IE_Compound] = field(default_factory=dict)
    sent_fars: Dict[int, pfcp.IE_Compound] = field(default_factory=dict)
    sent_urrs: Dict[int, pfcp.IE_Compound] = field(default_factory=dict)
    sent_qers: Dict[int, pfcp.IE_Compound] = field(default_factory=dict)


# Global non-constants
sock: Optional[socket.socket] = None
our_addr: str = ""
peer_addr: str = ""
sequence_number: int = 0
thread_lock = Lock()
association_established = False
script_terminating = False
active_sessions: Dict[int, Session] = {}
pcap_filename: Optional[str] = None
# End global non-constants


def capture(pkt: scapy.Packet):
    if pcap_filename:
        wrpcap(pcap_filename, pkt, append=True)


def already_sent(rule_id: int, rule: pfcp.IE_Compound, sent_rule_map: Dict[int, pfcp.IE_Compound]):
    """
    Check if the soon-to-be-sent rule has been previously sent to the PFCP agent,
    and then optimistically store the rule as sent
    :param rule_id: the ID of the rule that will soon be sent
    :param rule: the rule that will soon be sent
    :param sent_rule_map: the rule_id->rule mapping of previously sent rules
    :return: None
    """
    previous_rule = sent_rule_map.get(rule_id, None)
    sent_rule_map[rule_id] = rule
    if previous_rule is None:
        return False
    return previous_rule == rule


def clear_sent_rules():
    """
    Wipe out the cache of previously sent rules
    :return: None
    """
    sent_pdrs.clear()
    sent_fars.clear()
    sent_urrs.clear()
    sent_qers.clear()


def get_addresses_from_prefix(prefix: IPv4Network,
                              count: int) -> Generator[IPv4Address, None, None]:
    """
    Generator for yielding Ip4Addresses from the provided prefix.
    :param prefix: the prefix from which addresses should be generated
    :param count: how many addresses to yield
    :return: an address generator
    """
    # Currently this doesn't allow the address with host bits all 0,
    #  so the first host address is (prefix_addr & mask) + 1
    if count >= 2 ** (prefix.max_prefixlen - prefix.prefixlen):
        raise Exception("trying to generate more addresses than a prefix contains!")
    base_addr = ip2int(prefix.network_address) + 1
    offset = 0
    while offset < count:
        yield IPv4Address(base_addr + offset)
        offset += 1


def ip2int(addr: IPv4Address):
    return struct.unpack("!I", addr.packed)[0]


def int2ip(addr: int):
    return IPv4Address(addr)


def get_sequence_num(reset=False):
    """
    Generate a sequence number for a PFCP message.
    :param reset: if true, resets the sequence number counter
    :return: a sequence number to be used in a PFCP message
    """
    thread_lock.acquire()
    global sequence_number
    if reset:
        sequence_number = 0
    sequence_number += 1
    thread_lock.release()
    return sequence_number


def open_socket(our_addr: str):
    sock = socket.socket(
        socket.AF_INET,  # Internet
        socket.SOCK_DGRAM)  # UDP
    sock.bind((our_addr, UDP_PORT_PFCP))
    print("Socket opened ")
    return sock


def craft_fseid(seid: int, address: str) -> pfcp.IE_Compound:
    fseid = pfcp.IE_FSEID()
    fseid.v4 = 1
    fseid.seid = seid
    fseid.ipv4 = address
    return fseid


def craft_pdr(session: Session, id: int, far_id: int, qer_id: int, urr_id: int, src_iface: int,
              ue_addr: IPv4Address = None, from_tunnel=False, tunnel_dst: str = None,
              teid: int = None, precedence=2) -> pfcp.IE_Compound:
    pdr = pfcp.IE_UpdatePDR() if id in session.sent_pdrs else pfcp.IE_CreatePDR()
    pdr_id = pfcp.IE_PDR_Id()
    pdr_id.id = id
    pdr.IE_list.append(pdr_id)
    _precedence = pfcp.IE_Precedence()
    _precedence.precedence = precedence
    pdr.IE_list.append(_precedence)

    # Packet Detection Information
    pdi = pfcp.IE_PDI()

    # Source interface
    source_interface = pfcp.IE_SourceInterface()
    source_interface.interface = src_iface
    pdi.IE_list.append(source_interface)

    if from_tunnel:
        if tunnel_dst is None or teid is None:
            raise Exception("ERROR: tunnel dst and teid should be provided for tunnel PDR")
        # Add the F-TEID to the PDI
        fteid = pfcp.IE_FTEID()
        fteid.V4 = 1
        fteid.TEID = teid
        fteid.ipv4 = tunnel_dst
        pdi.IE_list.append(fteid)
        # Add outer header removal instruction to PDR
        outer_header_removal = pfcp.IE_OuterHeaderRemoval()
        outer_header_removal.header = 0
        pdr.IE_list.append(outer_header_removal)
    else:
        if ue_addr is None:
            raise Exception("UE address required for downlink PDRs!")
        # Add UE IPv4 address to the PDI
        _ue_addr = pfcp.IE_UE_IP_Address()
        _ue_addr.V4 = 1
        _ue_addr.ipv4 = ue_addr
        pdi.IE_list.append(_ue_addr)
        # If its not from a tunnel, then its from the internet
        net_instance = pfcp.IE_NetworkInstance()
        net_instance.instance = "internetinternetinternetinterne"
        pdi.IE_list.append(net_instance)

    # Add a fully wildcard SDF filter
    sdf = pfcp.IE_SDF_Filter()
    sdf.FD = 1
    sdf.flow_description = "0.0.0.0/0 0.0.0.0/0 0 : 65535 0 : 65535 0x0/0x0"
    pdi.IE_list.append(sdf)

    pdr.IE_list.append(pdi)

    # Add all rule IDs
    _far_id = pfcp.IE_FAR_Id()
    _far_id.id = far_id
    _qer_id = pfcp.IE_QER_Id()
    _qer_id.id = qer_id
    _urr_id = pfcp.IE_URR_Id()
    _urr_id.id = urr_id
    pdr.IE_list.append(_far_id)
    pdr.IE_list.append(_qer_id)
    pdr.IE_list.append(_urr_id)

    return pdr


def craft_far(session: Session, id: int, forward_flag=False, drop_flag=False, buffer_flag=False,
              dst_iface: int = None, tunnel=False, tunnel_dst: str = None,
              teid: int = None) -> pfcp.IE_Compound:
    far = pfcp.IE_UpdateFAR() if id in session.sent_fars else pfcp.IE_CreateFAR()
    far_id = pfcp.IE_FAR_Id()
    far_id.id = id
    far.IE_list.append(far_id)

    # Apply Action
    apply_action = pfcp.IE_ApplyAction()
    apply_action.FORW = int(forward_flag)
    apply_action.DROP = int(drop_flag)
    apply_action.BUFF = int(buffer_flag)
    far.IE_list.append(apply_action)

    # Forwarding Parameters
    forward_param = pfcp.IE_ForwardingParameters(
    ) if not update else pfcp.IE_UpdateForwardingParameters()
    _dst_iface = pfcp.IE_DestinationInterface()
    _dst_iface.interface = dst_iface
    forward_param.IE_list.append(_dst_iface)

    if tunnel:
        if (not buffer_flag) and tunnel_dst is None or teid is None:
            raise Exception("ERROR: tunnel dst and teid should be provided for tunnel FAR")
        outer_header = pfcp.IE_OuterHeaderCreation()
        outer_header.GTPUUDPIPV4 = 1
        outer_header.ipv4 = tunnel_dst
        outer_header.TEID = teid if not buffer_flag else 0  # FARs that buffer have a TEID of zero
        forward_param.IE_list.append(outer_header)

    far.IE_list.append(forward_param)
    return far


def craft_qer(session: Session, id: int, max_bitrate_up=12345678, max_bitrate_down=12345678,
              guaranteed_bitrate_up=12345678, guaranteed_bitrate_down=12345678) -> pfcp.IE_Compound:
    qer = pfcp.IE_UpdateQER() if id in session.sent_qers else pfcp.IE_CreateQER()
    # QER ID
    qer_id = pfcp.IE_QER_Id()
    qer_id.id = id
    qer.IE_list.append(qer_id)
    # Gate Status
    gate1 = pfcp.IE_GateStatus()
    qer.IE_list.append(gate1)
    # Maximum Bitrate
    max_bitrate = pfcp.IE_MBR()
    max_bitrate.ul = max_bitrate_up
    max_bitrate.dl = max_bitrate_down
    qer.IE_list.append(max_bitrate)
    # Guaranteed Bitrate
    guaranteed_bitrate = pfcp.IE_GBR()
    guaranteed_bitrate.ul = guaranteed_bitrate_up
    guaranteed_bitrate.dl = guaranteed_bitrate_down
    qer.IE_list.append(guaranteed_bitrate)
    return qer


def craft_urr(session: Session, id: int, quota: int, threshold: int) -> pfcp.IE_Compound:
    urr = pfcp.IE_UpdateURR() if id in session.sent_urrs else pfcp.IE_CreateURR()
    # URR ID
    urr_id = pfcp.IE_URR_Id()
    urr_id.id = id
    urr.IE_list.append(urr_id)
    # Measurement Method
    measure_method = pfcp.IE_MeasurementMethod()
    measure_method.VOLUM = 1
    urr.IE_list.append(measure_method)
    # Report trigger
    report_trigger = pfcp.IE_ReportingTriggers()
    report_trigger.volume_threshold = 1
    report_trigger.volume_quota = 1
    urr.IE_list.append(report_trigger)
    # Volume quota
    volume_quota = pfcp.IE_VolumeQuota()
    volume_quota.TOVOL = 1
    volume_quota.total = quota
    urr.IE_list.append(volume_quota)
    # Volume threshold
    volume_threshold = pfcp.IE_VolumeThreshold()
    volume_threshold.TOVOL = 1
    volume_threshold.total = threshold
    urr.IE_list.append(volume_threshold)

    return urr


def craft_pfcp_association_setup_packet() -> scapy.Packet:
    # create PFCP packet
    pfcp_header = pfcp.PFCP()
    # create setup request packet
    setup_request = pfcp.PFCPAssociationSetupRequest()
    setup_request.version = 1
    # Let's add IEs into the message
    ie1 = pfcp.IE_NodeId()
    ie1.ipv4 = our_addr
    setup_request.IE_list.append(ie1)
    ie2 = pfcp.IE_RecoveryTimeStamp()
    setup_request.IE_list.append(ie2)
    return IP(src=our_addr, dst=peer_addr) / UDP() / pfcp_header / setup_request


def craft_pfcp_association_release_packet() -> scapy.Packet:
    pfcp_header = pfcp.PFCP()
    # create release request packet
    release_request = pfcp.PFCPAssociationReleaseRequest()
    release_request.version = 1
    # Let's add IEs into the message
    ie1 = pfcp.IE_NodeId()
    ie1.ipv4 = our_addr
    release_request.IE_list.append(ie1)
    return IP(src=our_addr, dst=peer_addr) / UDP() / pfcp_header / release_request


def add_rules_to_request(args: argparse.Namespace, request, update=False, add_pdrs=False,
                         add_fars=False, add_urrs=False, add_qers=False, force_add=False) -> None:
    rule_count = args.ue_count * 2
    ue_addr_gen = get_addresses_from_prefix(args.ue_pool, args.ue_count)
    seid_gen = iter(range(args.seid_base, args.seid_base + rule_count))
    teid_gen = iter(range(args.teid_base, args.teid_base + rule_count))
    far_id_gen = iter(range(args.far_base, args.far_base + rule_count))
    pdr_id_gen = iter(range(args.pdr_base, args.pdr_base + rule_count))
    urr_id_gen = iter(range(args.urr_base, args.urr_base + rule_count))
    qer_id_gen = iter(range(args.urr_base, args.urr_base + rule_count))

    for ue_index in range(args.ue_count):
        seid = next(seid_gen)
        ue_addr = next(ue_addr_gen)
        teid1 = next(teid_gen)
        teid2 = next(teid_gen)

        pdr_id1 = next(pdr_id_gen)
        pdr_id2 = next(pdr_id_gen)

        far_id1 = next(far_id_gen)
        far_id2 = next(far_id_gen)

        urr_id1 = next(urr_id_gen)
        urr_id2 = next(urr_id_gen)

        qer_id1 = next(qer_id_gen)
        qer_id2 = next(qer_id_gen)

        if add_pdrs:
            # uplink
            pdr1 = craft_pdr(id=pdr_id1, far_id=far_id1, qer_id=qer_id1, urr_id=urr_id1,
                             src_iface=IFACE_ACCESS, from_tunnel=True, teid=teid1,
                             tunnel_dst=args.s1u_addr, update=update,
                             precedence=args.pdr_precedence)
            if force_add or not already_sent(pdr_id1, pdr1, sent_pdrs):
                request.IE_list.append(pdr1)
            # downlink
            pdr2 = craft_pdr(id=pdr_id2, far_id=far_id2, qer_id=qer_id2, urr_id=urr_id2,
                             src_iface=IFACE_CORE, from_tunnel=False, ue_addr=ue_addr,
                             update=update)
            if force_add or not already_sent(pdr_id2, pdr2, sent_pdrs):
                request.IE_list.append(pdr2)

        if add_fars:
            # uplink
            far1 = craft_far(id=far_id1, update=update, forward_flag=True, dst_iface=IFACE_CORE,
                             tunnel=False)
            if force_add or not already_sent(far_id1, far1, sent_fars):
                request.IE_list.append(far1)
            # The downlink FAR should only tunnel if this is an update message. Our PFCP agent does not support
            #  outer header creation on session establishment, only session modification
            # downlink
            far2 = craft_far(id=far_id2, update=update, forward_flag=True, dst_iface=IFACE_ACCESS,
                             tunnel=update, tunnel_dst=args.enb_addr, teid=teid2,
                             buffer_flag=args.buffer)
            if force_add or not already_sent(far_id2, far2, sent_fars):
                request.IE_list.append(far2)

        if add_qers:
            qer1 = craft_qer(id=qer_id1, update=update)
            if force_add or not already_sent(qer_id1, qer1, sent_qers):
                request.IE_list.append(qer1)
            qer2 = craft_qer(id=qer_id2, update=update)
            if force_add or not already_sent(qer_id2, qer2, sent_qers):
                request.IE_list.append(qer2)

        if add_urrs:
            urr1 = craft_urr(id=urr_id1, quota=100000, threshold=40000, update=update)
            if force_add or not already_sent(urr_id1, urr1, sent_urrs):
                request.IE_list.append(urr1)
            urr2 = craft_urr(id=urr_id2, quota=100000, threshold=50000, update=update)
            if force_add or not already_sent(urr_id2, urr2, sent_urrs):
                request.IE_list.append(urr2)


def craft_pfcp_session_est_packet(args: argparse.Namespace) -> scapy.Packet:
    pfcp_header = pfcp.PFCP()
    pfcp_header.version = 1
    pfcp_header.S = 1
    pfcp_header.message_type = MSG_TYPES["session_establishment_request"]
    pfcp_header.seid = 0
    pfcp_header.seq = get_sequence_num()

    establishment_request = pfcp.PFCPSessionEstablishmentRequest()
    # add IEs into message
    nodeid = pfcp.IE_NodeId()
    nodeid.ipv4 = our_addr
    establishment_request.IE_list.append(nodeid)

    fseid = craft_fseid(our_seid, our_addr)
    establishment_request.IE_list.append(fseid)

    pdn_type = pfcp.IE_PDNType()
    establishment_request.IE_list.append(pdn_type)

    add_rules_to_request(args=args, request=establishment_request, update=False, add_pdrs=True,
                         add_fars=True, add_urrs=True, add_qers=True)

    return IP(src=our_addr, dst=peer_addr) / UDP() / pfcp_header / establishment_request


def craft_pfcp_session_modify_packet(args: argparse.Namespace) -> scapy.Packet:
    # fill pfcp header
    pfcp_header = pfcp.PFCP()
    pfcp_header.version = 1
    pfcp_header.S = 1
    pfcp_header.message_type = MSG_TYPES["session_modification_request"]
    if peer_seid == -1:
        raise Exception("Peer SEID has not yet been received.")
    pfcp_header.seid = peer_seid
    pfcp_header.seq = get_sequence_num()

    modification_request = pfcp.PFCPSessionModificationRequest()
    fseid = craft_fseid(our_seid, our_addr)
    modification_request.IE_list.append(fseid)

    add_rules_to_request(args, modification_request, update=True, add_fars=True)

    return IP(src=our_addr, dst=peer_addr) / UDP() / pfcp_header / modification_request


def craft_pfcp_session_delete_packet(session: Session) -> scapy.Packet:
    pfcp_header = pfcp.PFCP()
    pfcp_header.version = 1
    pfcp_header.S = 1
    pfcp_header.message_type = MSG_TYPES["session_deletion_request"]
    if session.peer_seid is None:
        raise Exception("Peer SEID has not yet been received.")
    pfcp_header.seid = session.peer_seid
    pfcp_header.seq = get_sequence_num()

    deletion_request = pfcp.PFCPSessionDeletionRequest()
    fseid = craft_fseid(session.our_seid, our_addr)
    deletion_request.IE_list.append(fseid)

    delete_pkt = IP(src=our_addr, dst=peer_addr) / UDP() / pfcp_header / deletion_request
    return delete_pkt


def send_recv_pfcp(pkt: scapy.Packet, expected_response_type: int, verbosity=0) -> None:
    """
    Send the given PFCP packet out the global socket, and wait for a response with the given PFCP message type.
    :param pkt: The packet to be sent out the global socket
    :param expected_response_type: The expected PFCP message type of the response
    :param verbosity: 0 for no printing, 1 to print some stuff, 2 to print the dissected sent and received packets
    :return: None
    """
    global peer_seid

    if verbosity > 1:
        pkt.show()
    capture(pkt)
    scapy.send(pkt, verbose=verbosity)
    data, addr = sock.recvfrom(1024)  # buffer size is 1024 bytes
    if verbosity > 0:
        print("Received message: %s" % data)
    response = pfcp.PFCP()
    response.dissect(data)
    capture(response)
    if verbosity > 1:
        response.show()
    if response.message_type == expected_response_type:
        for ie in response.payload.IE_list:
            if verbosity > 0:
                if ie.ie_type == 60:
                    print("decoded node type : ", ie)
                elif ie.ie_type == 19:
                    print("decoded cause : ", ie.cause)
                elif ie.ie_type == 57:
                    print("FSEID recieved ")
                    print("FSEID seid {} and ip address {} ", ie.seid, ie.ipv4)
                elif ie.ie_type == 96:
                    print("recovery timestamp received")
                elif ie.ie_type == 116:
                    print("decoded ip resource information received")
            if ie.ie_type == 57:
                peer_seid = int(ie.seid)
    else:
        print("ERROR: Expected response of type %s but received %s" %
              (pfcp.PFCPmessageType[expected_response_type],
               pfcp.PFCPmessageType[response.message_type]))


def setup_pfcp_association(args: argparse.Namespace) -> None:
    get_sequence_num(reset=True)  # zero out the sequence number
    global association_established
    pkt = craft_pfcp_association_setup_packet()
    send_recv_pfcp(pkt, MSG_TYPES["association_setup_response"])
    association_established = True  # signal the heartbeat thread to start sending


def teardown_pfcp_association(args: argparse.Namespace) -> None:
    global association_established
    pkt = craft_pfcp_association_release_packet()
    send_recv_pfcp(pkt, MSG_TYPES["association_release_response"])
    association_established = False


def create_pfcp_sessions(args: argparse.Namespace) -> None:
    session_ids = range(args.seid_base, args.seid_base + args.ue_count)
    pkt = craft_pfcp_session_est_packet(args)
    send_recv_pfcp(pkt, MSG_TYPES["session_establishment_response"])


def modify_pfcp_sessions(args: argparse.Namespace) -> None:
    pkt = craft_pfcp_session_modify_packet(args)
    send_recv_pfcp(pkt, MSG_TYPES["session_modification_response"])


def delete_pfcp_sessions(args: argparse.Namespace) -> None:
    for seid in range(args.seid_base, args.seid_base + args.ue_count):
        session = active_sessions[seid]
        pkt = craft_pfcp_session_delete_packet(session)
    send_recv_pfcp(pkt, MSG_TYPES["session_deletion_response"])
    clear_sent_rules()


def send_pfcp_heartbeats() -> None:
    while True:
        for _ in range(HEARTBEAT_PERIOD):
            # semi-busy wait
            time.sleep(1)
            if script_terminating:
                return
        if not association_established:
            # Don't heartbeat unless an association is currently established
            continue
        pfcp_header = pfcp.PFCP()
        pfcp_header.version = 1
        pfcp_header.S = 0  # SEID flag false
        pfcp_header.seq = get_sequence_num()
        pfcp_header.message_type = MSG_TYPES["heartbeat_request"]

        heartbeat = pfcp.PFCPHeartbeatRequest()
        heartbeat.version = 1
        heartbeat.IE_list.append(pfcp.IE_RecoveryTimeStamp())

        pkt = IP(src=our_addr, dst=peer_addr) / UDP() / pfcp_header / heartbeat
        send_recv_pfcp(pkt, MSG_TYPES["heartbeat_response"], verbosity=0)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # This override stops the argument parser from calling exit() on error
        raise Exception("Bad parser input: %s" % message)


def interrupt_association(args: argparse.Namespace) -> None:
    global association_established
    association_established = False  # stop the heartbeat thread
    active_sessions.clear()


def terminate(args: argparse.Namespace) -> None:
    global script_terminating
    if association_established:
        print("Exiting before association deleted. Deleting..")
        delete_pfcp_sessions(args)
    script_terminating = True
    exit()


def handle_user_input(input_file: Optional[IO] = None, output_file: Optional[IO] = None) -> None:
    global script_terminating

    user_choices = {
        "associate": ("Setup PFCP Association", setup_pfcp_association),
        "create": ("Create PFCP Session(s)", create_pfcp_sessions),
        "modify": ("Modify PFCP Session(s)", modify_pfcp_sessions),
        "delete": ("Delete PFCP Session(s)", delete_pfcp_sessions),
        "disassociate": ("Teardown PFCP Association", teardown_pfcp_association),
        "interrupt": ("Ungracefully teardown PFCP association", interrupt_association),
        "stop": ("Exit script", terminate)
    }

    parser = ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("choice", type=str, help="The PFCP client operation to perform",
                        choices=user_choices.keys())
    parser.add_argument("--buffer", action='store_true',
                        help="If this argument is present, downlink FARs will have the buffering flag set to true")
    parser.add_argument("--session-count", type=int, default=1,
                        help="The number of sessions for which UE flows should be created.")
    parser.add_argument("--ue-pool", type=IPv4Network, default=IPv4Network("17.0.0.0/24"),
                        help="The IPv4 prefix from which UE addresses will be drawn.")
    parser.add_argument("--s1u-addr", type=IPv4Address, default=IPv4Address("140.0.100.254"),
                        help="The IPv4 address of the UPF's S1U interface")
    parser.add_argument("--enb-addr", type=IPv4Address, default=IPv4Address("140.0.100.1"),
                        help="The IPv4 address of the eNodeB")
    parser.add_argument(
        "--seid-base", type=int, default=1, help="The first SEID to use for the first UE session. " +
                                                 "Further SEIDs will be generated by incrementing.")
    parser.add_argument(
        "--teid-base", type=int, default=255, help="The first TEID to use for the first UE flow. " +
                                                   "Further TEIDs will be generated by incrementing.")
    parser.add_argument(
        "--pdr-base", type=int, default=1, help="The first PDR ID to use for the first UE of each session. " +
                                                "Further PDR IDs will be generated by incrementing.")
    parser.add_argument(
        "--far-base", type=int, default=1, help="The first FAR ID to use for the first UE of each session. " +
                                                "Further FAR IDs will be generated by incrementing.")
    parser.add_argument(
        "--urr-base", type=int, default=1, help="The first URR ID to use for the first UE of each session. " +
                                                "Further URR IDs will be generated by incrementing.")
    parser.add_argument(
        "--qer-base", type=int, default=1, help="The first QER ID to use for the first UE of each session. " +
                                                "Further QER IDs will be generated by incrementing.")
    parser.add_argument("--pdr-precedence", type=int, default=2,
                        help="The priority/precedence of PDRs.")

    def get_user_input():
        if not input_file:
            return input("Enter your selection : ")
        else:
            # Poll the input file for an unread line
            while True:
                read_head = input_file.tell()
                line = input_file.readline()
                if line:
                    return line
                time.sleep(0.5)
                input_file.seek(read_head)

    while True:
        for choice, (action_desc, action) in user_choices.items():
            print("\"%s\" - %s" % (choice, action_desc))
        try:

            args = parser.parse_args(input("Enter your selection : ").split())
        except Exception as e:
            print(e)
            parser.print_help()
            continue
        try:
            choice_desc, choice_func = user_choices[args.choice]
            print("Selected %s" % choice_desc)
            choice_func(args)
        except Exception as e:
            # Catch the exception just long enough to signal the heartbeat thread to end
            script_terminating = True
            raise e


def main():
    global our_addr, peer_addr, sock, pcap_filename

    our_addr = ifcfg.interfaces()['eth0']['inet']
    sock = open_socket(our_addr)

    parser = argparse.ArgumentParser()
    parser.add_argument("upfaddr", help="Address or hostname of the UPF")
    parser.add_argument("--input-file", help="File to poll for input commands. Default is stdin")
    parser.add_argument("--output-file", help="File in which to write output. Default is stdout")
    parser.add_argument("--pcap-file", help="File in which to write sent/received PFCP packets. Default is no capture")
    args = parser.parse_args()
    input_file: Optional[IO] = None
    output_file: Optional[IO] = None
    # Try opening the files right now so we don't wait until the main loop to fail
    if args.input_file:
        input_file = open(args.input_file, "r")
    if args.output_file:
        output_file = open(args.output_file, "w")
    if args.pcap_file:
        # clear the pcap file
        pcap_filename = args.pcap_file
        open(pcap_filename, 'w').close()

    try:
        peer_addr = socket.gethostbyname(args.upfaddr)
    except socket.gaierror as e:
        try:
            peer_addr = str(IPv4Address(args.upfaddr))
        except AddressValueError as e:
            print("Argument must be a valid hostname or address")
            exit(1)

    thread1 = Thread(target=handle_user_input, args=(input_file, output_file))
    thread2 = Thread(target=send_pfcp_heartbeats)

    thread1.start()
    thread2.start()
    thread1.join()
    thread2.join()


if __name__ == "__main__":
    main()
