"""Exercise the production pidfd guard with real processes and Unix peers."""
import json
from pathlib import Path
import os
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import unittest


class ResidentOwnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='resident-owner-build-')
        root = Path(cls.build.name)
        source = Path(__file__).with_name('resident_control.c').resolve()
        harness = root / 'owner.c'
        harness.write_text('#define main resident_program_main\n#include ' + json.dumps(str(source)) + '''
#undef main
int main(int argc,char **argv) {
 check(argc==2||argc==3,"test directory");strcpy(directory,argv[1]);path(socket_path,sizeof(socket_path),"api.sock");
 if(argc==3) {
  struct metadata_file file=prepare_metadata(argv[2]);char data[1024];puts("READY");fflush(stdout);
  while(fgets(data,sizeof(data),stdin)) {overwrite_metadata(&file,data,strlen(data));puts("WRITTEN");fflush(stdout);}
  return 0;
 }
 prepare_owner();puts("READY");fflush(stdout);
 if(getchar()=='a')connect_api();else (void)owner_pid();
 puts("ALIVE");return 0;
}
''')
        cls.binary = root / 'owner'
        subprocess.run(['gcc','-O2','-Wall','-Wextra','-Werror',str(harness),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='resident-owner-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def child(self, *, api=False):
        code = ('import socket,sys,time\ns=socket.socket(socket.AF_UNIX);s.bind(sys.argv[1]);'
                's.listen(1);print("READY",flush=True);c,_=s.accept();time.sleep(60)' if api else
                'import time;print("READY",flush=True);time.sleep(60)')
        process = subprocess.Popen([sys.executable,'-u','-c',code,str(self.root/'api.sock')],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop,process)
        self.assertEqual(process.stdout.readline().strip(),'READY')
        ticks = Path(f'/proc/{process.pid}/stat').read_text().rsplit(')',1)[1].split()[19]
        (self.root/'fc.pid').write_text(f'{process.pid} {ticks}\n')
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)

    def guard(self, *, metadata=False, **options):
        argv=[str(self.binary),str(self.root)]+(['data.json'] if metadata else [])
        process = subprocess.Popen(argv,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,**options)
        self.addCleanup(self.stop,process)
        return process

    def test_live_owner_without_reopening_pid_file(self):
        self.child(); guard=self.guard()
        self.assertEqual(guard.stdout.readline().strip(),'READY')
        (self.root/'fc.pid').unlink()
        out,err=guard.communicate('p',timeout=5)
        self.assertEqual(guard.returncode,0,err);self.assertIn('ALIVE',out)

    def test_exited_owner_rejected(self):
        child=self.child();guard=self.guard()
        self.assertEqual(guard.stdout.readline().strip(),'READY')
        self.stop(child)
        _,err=guard.communicate('p',timeout=5)
        self.assertNotEqual(guard.returncode,0);self.assertIn('prepared owner exited',err)

    def test_wrong_starttime_rejected_before_ready(self):
        child=self.child();(self.root/'fc.pid').write_text(f'{child.pid} 0\n')
        guard=self.guard();out,err=guard.communicate(timeout=5)
        self.assertNotEqual(guard.returncode,0);self.assertNotIn('READY',out);self.assertIn('owner PID reused',err)

    def test_api_belongs_to_pinned_owner(self):
        self.child(api=True);guard=self.guard()
        self.assertEqual(guard.stdout.readline().strip(),'READY')
        _,err=guard.communicate('a',timeout=5)
        self.assertEqual(guard.returncode,0,err)

    def test_api_of_another_process_rejected(self):
        self.child()
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.root/'api.sock'));listener.listen(1)
            guard=self.guard();self.assertEqual(guard.stdout.readline().strip(),'READY')
            _,err=guard.communicate('a',timeout=5)
        self.assertNotEqual(guard.returncode,0);self.assertIn('API belongs to another process',err)

    def test_metadata_reuses_inode_and_removes_old_tail(self):
        guard=self.guard(metadata=True);self.assertEqual(guard.stdout.readline().strip(),'READY')
        path=self.root/'data.json';inode=path.stat().st_ino
        for data in ('longer metadata record\n','x\n','another longer record\n'):
            guard.stdin.write(data);guard.stdin.flush()
            self.assertEqual(guard.stdout.readline().strip(),'WRITTEN')
            self.assertEqual(path.read_text(),data);self.assertEqual(path.stat().st_ino,inode)
        _,err=guard.communicate(timeout=5);self.assertEqual(guard.returncode,0,err)

    def test_metadata_symlink_rejected_without_modifying_target(self):
        target=self.root/'target';target.write_text('preserved')
        (self.root/'data.json').symlink_to(target)
        guard=self.guard(metadata=True);out,_=guard.communicate('changed\n',timeout=5)
        self.assertNotEqual(guard.returncode,0);self.assertNotIn('READY',out)
        self.assertEqual(target.read_text(),'preserved')

    def test_metadata_hardlink_rejected(self):
        target=self.root/'target';target.write_text('preserved')
        os.link(target,self.root/'data.json')
        guard=self.guard(metadata=True);out,_=guard.communicate('changed\n',timeout=5)
        self.assertNotEqual(guard.returncode,0);self.assertNotIn('READY',out)
        self.assertEqual(target.read_text(),'preserved')

    def test_partial_metadata_write_is_not_reported_complete(self):
        def limit():
            signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE,(8,8))
        guard=self.guard(metadata=True,preexec_fn=limit)
        self.assertEqual(guard.stdout.readline().strip(),'READY')
        out,err=guard.communicate('a record longer than eight bytes\n',timeout=5)
        self.assertNotEqual(guard.returncode,0);self.assertNotIn('WRITTEN',out)
        self.assertIn('metadata write',err)


if __name__=='__main__':
    unittest.main()
