"""Framed commands on an owned QEMU virtio-serial channel; no console polling."""
import secrets
import socket
import struct
import time

MAGIC = 0x43584331
REQUEST = struct.Struct('<IIQII32s')
REPLY = struct.Struct('<IiQQQ')
CAP = 64 * 1024 * 1024
_clients = {}


def _read(sock, count):
    out = bytearray()
    while len(out) < count:
        data = sock.recv(count-len(out))
        if not data:
            raise RuntimeError('Control channel closed; command was not retried')
        out.extend(data)
    return out


def call(vm, operation, body, timeout):
    if len(body) > CAP or not .001 <= timeout <= 900:
        raise ValueError('Control frame bounds')
    key = (vm['control_rpc'], vm['pid'], vm['starttime'])
    sock = _clients.get(key)
    started = time.monotonic_ns()
    try:
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout+5)
            sock.connect(vm['control_rpc'])
            _clients[key] = sock
        # Leave room for the guest's bounded command to return a timeout reply.
        sock.settimeout(timeout+5)
        sequence = secrets.randbits(64)
        nonce = vm['control_nonce'].encode()
        if len(nonce) > 32:
            raise ValueError('Control nonce bounds')
        sock.sendall(REQUEST.pack(MAGIC, operation, sequence, len(body), int(timeout*1000), nonce)+body)
        magic, status, received, length, native = REPLY.unpack(_read(sock, REPLY.size))
        if magic != MAGIC or received != sequence or length > CAP:
            raise RuntimeError('Wrong control reply identity/bounds')
        data = bytes(_read(sock, length))
    except BaseException:
        if sock is not None:
            sock.close()
        _clients.pop(key, None)
        raise
    text = data.decode(errors='replace')
    if status:
        raise RuntimeError(f'Guest RPC command failed ({status}):\n{text}')
    # Expose transport/native boundaries without issuing another command.
    vm['_last_control_timing'] = {'wall_ns':time.monotonic_ns()-started, 'native_ns':native,
                                  'request_bytes':len(body), 'response_bytes':length}
    return text


def command(vm, body, timeout):
    return 'LAB_RPC_COMMAND '+repr(body)+'\n'+call(vm, 1, ('set -e\n'+body+'\n').encode(), timeout)


def put(vm, path, data, after='', timeout=120):
    path = path.encode()
    script = ('set -e\n'+after+'\n').encode() if after else b''
    return 'LAB_RPC_PUT '+repr((path.decode(), after))+'\n'+call(vm, 2,
        struct.pack('<II',len(path),len(script))+path+script+data, timeout)
