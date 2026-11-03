#!/usr/bin/env python3
"""Check Python's request/reply layout against the compiled C protocol."""
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
import resident_proxy as proxy

class Wire(unittest.TestCase):
    def test_c_layout_and_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'wire.c';binary=root/'wire'
            source.write_text(r'''
#include <stdio.h>
#include <string.h>
#include "resident_wire.h"
int main(void) {
 struct request q;memset(&q,0,sizeof(q));q.magic=RESIDENT_MAGIC;q.first=1;q.operation=1;
 q.vm=4;q.epoch=9;q.canonical_start=4096;q.canonical_bytes=8192;
 strcpy(q.nonce,"0123456789abcdef0123456789abcdef");strcpy(q.label,"example");strcpy(q.peer,"172.30.0.2");
 fwrite(&q,sizeof(q),1,stdout);
 struct outcome o;memset(&o,0,sizeof(o));o.send.us=12.125;o.send.status=204;
 o.target.magic=RESIDENT_MAGIC;o.target.receive.us=44.5;o.target.receive.status=204;
 o.handoff_ns=100;o.connection_ns=40;o.pause_to_resume_ns=90;o.state_len=300;o.manifest_len=400;
 uint64_t times[2]={200,180};uint32_t magic=RESIDENT_MAGIC;
 fwrite(times,sizeof(times),1,stdout);fwrite(&magic,sizeof(magic),1,stdout);fwrite(&o,sizeof(o),1,stdout);
 return 0;
}
''')
            subprocess.run(['gcc','-Wall','-Wextra','-Werror','-I',str(Path(__file__).resolve().parent),str(source),'-o',str(binary)],check=True)
            data=subprocess.check_output([binary])
        expected=proxy.REQUEST.pack(proxy.MAGIC,0,0,1,1,0,0,4,9,4096,8192,
            b'0123456789abcdef0123456789abcdef',b'example',b'172.30.0.2')
        self.assertEqual(data[:proxy.REQUEST.size],expected)
        reply=data[proxy.REQUEST.size:];value=proxy.decode(reply)
        self.assertEqual(value['request_to_resume_response_ns'],180)
        self.assertEqual(value['apis'],{'send':{'status':204,'duration_us':12.125},'receive':{'status':204,'duration_us':44.5}})
        self.assertEqual(value['state_bytes'],300);self.assertEqual(value['manifest_bytes'],400)
        proxy.verify_receipt('RESIDENT_PROXY_WIRE '+reply.hex(),value)
        changed=dict(value,request_ns=1)
        with self.assertRaises(ValueError):proxy.verify_receipt('RESIDENT_PROXY_WIRE '+reply.hex(),changed)
        bad=bytearray(reply);struct.pack_into('<I',bad,16,0)
        with self.assertRaises(RuntimeError):proxy.decode(bad)
        with self.assertRaises(RuntimeError):proxy.decode(reply[:-1])

if __name__=='__main__':unittest.main()
