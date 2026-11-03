"""Send an already prepared resident request without launching a guest process."""
import json
import struct

import control_rpc

MAGIC=0x43585233
REQUEST=struct.Struct('<7I4x4Q33s96s16s7x')
OUTCOME=struct.Struct('<di4xIIdi4xQQQQII')
TIMES=struct.Struct('<QQ')
assert REQUEST.size==216 and OUTCOME.size==80


def decode(data):
    if len(data)!=TIMES.size+4+OUTCOME.size:
        raise RuntimeError('Resident reply size')
    request_ns,resume_ns=TIMES.unpack_from(data)
    marker,=struct.unpack_from('<I',data,TIMES.size)
    (send_us,send_status,magic,ack_len,receive_us,receive_status,ack_ns,
     handoff_ns,connection_ns,pause_ns,state_bytes,manifest_bytes)=OUTCOME.unpack_from(data,TIMES.size+4)
    if (marker!=MAGIC or magic!=MAGIC or ack_len or ack_ns or send_status!=204 or receive_status!=204
        or not 0<resume_ns<=request_ns or not 0<state_bytes<65536 or not 0<manifest_bytes<65536):
        raise RuntimeError('Resident reply identity/status/bounds')
    return {'version':2,'wire_version':3,'handoff_ns':handoff_ns,'connection_prepare_ns':connection_ns,
        'guest_ack_ns':0,'pause_to_resume_response_ns':pause_ns,'request_ns':request_ns,
        'request_to_resume_response_ns':resume_ns,'state_bytes':state_bytes,'manifest_bytes':manifest_bytes,
        'ack':None,'apis':{'send':{'status':send_status,'duration_us':round(send_us,3)},
                          'receive':{'status':receive_status,'duration_us':round(receive_us,3)}}}


def request(vm,directory,nonce,first,label,peer,vm_id,epoch,canonical_start,canonical_bytes):
    nonce,label,peer=(value.encode() for value in (nonce,label,peer))
    if len(nonce)!=32 or not 0<len(label)<90 or not 0<len(peer)<16:
        raise ValueError('Resident request strings')
    wire=REQUEST.pack(MAGIC,0,0,int(first),1,0,0,vm_id,epoch,canonical_start,canonical_bytes,nonce,label,peer)
    data=control_rpc.unix_exchange(vm,directory+'/control/request.sock',wire,4+OUTCOME.size,4)
    value=decode(data)
    return 'RESIDENT_PROXY_WIRE '+data.hex()+'\nRESIDENT_FAST_JSON '+json.dumps(value,separators=(',',':'))+'\n'


def verify_receipt(text, expected):
    """Offline evidence check; never called by the handoff path."""
    records=[line.removeprefix('RESIDENT_PROXY_WIRE ') for line in text.splitlines()
             if line.startswith('RESIDENT_PROXY_WIRE ')]
    if records and (len(records)!=1 or decode(bytes.fromhex(records[0]))!=expected):
        raise ValueError('Resident wire differs from recorded JSON')
