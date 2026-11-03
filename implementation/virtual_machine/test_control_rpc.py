#!/usr/bin/env python3
"""Exercise the actual framed C server with fragmented traffic and failures."""
import os
from pathlib import Path
import pty
import socket
import struct
import subprocess
import tempfile
import threading
import tty
import unittest

import control_rpc as rpc

HERE = Path(__file__).resolve().parent

class Protocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='control-rpc-test-')
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root/'server'
        subprocess.run(['gcc','-O2','-Wall','-Wextra','-Werror',str(HERE/'control_rpc.c'),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.master,self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.server = subprocess.Popen([str(self.binary),os.ttyname(self.slave),'abcd1234'],stdout=subprocess.PIPE)
        self.assertEqual(self.server.stdout.readline(),b'LAB_CONTROL_READY\n')
        self.sequence = 0

    def tearDown(self):
        self.server.terminate();self.server.wait(timeout=5)
        self.server.stdout.close();os.close(self.master);os.close(self.slave)

    def request(self,operation,body,timeout=1000,nonce=b'abcd1234'):
        self.sequence += 1
        frame = rpc.REQUEST.pack(rpc.MAGIC,operation,self.sequence,len(body),timeout,nonce)+body
        def send():
            # Force both header and payload reassembly, including large PUT.
            pos=0
            while pos<len(frame): pos+=os.write(self.master,frame[pos:pos+317])
        sender=threading.Thread(target=send);sender.start()
        def read(size):
            data=bytearray()
            while len(data)<size: data.extend(os.read(self.master,size-len(data)))
            return bytes(data)
        magic,status,sequence,size,native=rpc.REPLY.unpack(read(rpc.REPLY.size))
        output=read(size);sender.join(timeout=5)
        self.assertFalse(sender.is_alive());self.assertEqual(magic,rpc.MAGIC)
        self.assertEqual(sequence,self.sequence);self.assertGreater(native,0)
        return status,output

    def test_commands_failures_and_timeout_recovery(self):
        self.assertEqual(self.request(1,b'printf hello'),(0,b'hello'))
        self.assertEqual(self.request(1,b'exit 42'),(42,b''))
        self.assertEqual(self.request(1,b'sleep 5',timeout=20),(124,b''))
        self.assertEqual(self.request(1,b'printf recovered'),(0,b'recovered'))

    def test_nonce_and_invalid_frame_do_not_execute(self):
        path=self.root/'must-not-exist'
        path.unlink(missing_ok=True)
        self.assertEqual(self.request(1,f'touch {path}'.encode(),nonce=b'wrong')[0],125)
        self.assertFalse(path.exists())
        self.assertEqual(self.request(1,b'printf bad\0ignored')[0],125)
        self.assertEqual(self.request(2,struct.pack('<II',4000,0)+b'/tmp/x')[0],125)
        self.assertEqual(self.request(99,b'ignored')[0],125)
        self.assertEqual(self.request(1,b'printf ok'),(0,b'ok'))

    def test_binary_put_and_after_exactly_once(self):
        path=self.root/'data';count=self.root/'count'
        count.unlink(missing_ok=True)
        data=bytes(range(256))*4096
        script=f'printf x >> {count}; printf written'.encode();name=str(path).encode()
        body=struct.pack('<II',len(name),len(script))+name+script+data
        self.assertEqual(self.request(2,body),(0,b'written'))
        self.assertEqual(path.read_bytes(),data);self.assertEqual(count.read_bytes(),b'x')

    def test_put_refuses_final_symlink(self):
        target=self.root/'target';target.write_bytes(b'preserved')
        path=self.root/'link';path.symlink_to(target)
        name=str(path).encode()
        self.assertEqual(self.request(2,struct.pack('<II',len(name),0)+name+b'changed')[0],125)
        self.assertEqual(target.read_bytes(),b'preserved')

class Client(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='control-client-test-')
        self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.address=self.temp.name+'/control.sock';self.listener.bind(self.address);self.listener.listen(2)
        self.vm={'control_rpc':self.address,'pid':1,'starttime':'test','control_nonce':'abcd1234'}
        self.received=[];self.failures=[]

    def tearDown(self):
        for key,sock in list(rpc._clients.items()):
            if key[0]==self.address: sock.close();del rpc._clients[key]
        self.listener.close();self.temp.cleanup()

    def serve(self,reply):
        def worker():
            try:
                with self.listener.accept()[0] as sock:
                    sock.settimeout(5)
                    header=rpc.REQUEST.unpack(rpc._read(sock,rpc.REQUEST.size))
                    body=rpc._read(sock,header[3]);self.received.append((header,body))
                    reply(sock,header)
            except BaseException as exc: self.failures.append(exc)
        thread=threading.Thread(target=worker);thread.start();return thread

    def check_server(self,thread):
        thread.join(timeout=5);self.assertFalse(thread.is_alive());self.assertEqual(self.failures,[])
        # The client must not silently reconnect and repeat an ambiguous request.
        self.listener.settimeout(.03)
        with self.assertRaises(socket.timeout): self.listener.accept()
        self.assertEqual(len(self.received),1)

    def test_closed_reply_never_replays(self):
        thread=self.serve(lambda sock,header:None)
        with self.assertRaisesRegex(RuntimeError,'not retried'):rpc.command(self.vm,'mutate once',1)
        self.check_server(thread)

    def test_wrong_reply_identity(self):
        thread=self.serve(lambda sock,h:sock.sendall(rpc.REPLY.pack(rpc.MAGIC,0,h[2]^1,0,100)))
        with self.assertRaisesRegex(RuntimeError,'identity'):rpc.command(self.vm,'mutate once',1)
        self.check_server(thread)

    def test_reply_bounds(self):
        thread=self.serve(lambda sock,h:sock.sendall(rpc.REPLY.pack(rpc.MAGIC,0,h[2],rpc.CAP+1,100)))
        with self.assertRaisesRegex(RuntimeError,'bounds'):rpc.command(self.vm,'mutate once',1)
        self.check_server(thread)

    def test_fragmented_reply(self):
        def reply(sock,h):
            for byte in rpc.REPLY.pack(rpc.MAGIC,0,h[2],5,100)+b'hello':sock.sendall(bytes([byte]))
        thread=self.serve(reply)
        self.assertTrue(rpc.command(self.vm,'printf hello',1).endswith('hello'))
        self.check_server(thread)
        self.assertEqual(self.vm['_last_control_timing']['native_ns'],100)

if __name__=='__main__': unittest.main()
