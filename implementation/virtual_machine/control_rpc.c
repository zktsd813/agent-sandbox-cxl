// SPDX-License-Identifier: Apache-2.0
// Private host-to-L1 command channel. Never carries the migrating L2 RAM.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/epoll.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAGIC 0x43584331u
#define CAP (64u * 1024 * 1024)
struct request { uint32_t magic, operation; uint64_t sequence; uint32_t bytes, timeout_ms; char nonce[32]; };
struct reply { uint32_t magic; int32_t status; uint64_t sequence, bytes, elapsed_ns; };
_Static_assert(sizeof(struct request)==56 && sizeof(struct reply)==32,"wire layout");
static uint64_t now(void) { struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000+t.tv_nsec; }
static void die(const char *s) { perror(s);exit(1); }
static int transfer(int fd,void *p,size_t n,int writing,int timeout_ms) {
 while(n) {
  struct pollfd q={.fd=fd,.events=writing?POLLOUT:POLLIN};int rc;
  do {rc=poll(&q,1,timeout_ms);}while(rc<0&&errno==EINTR);
  if(rc<=0)return -1;
  ssize_t k=writing?write(fd,p,n):read(fd,p,n);
  if(k<0&&(errno==EINTR||errno==EAGAIN))continue;
  if(k<=0)return -1;
  p=(char *)p+k;n-=k;
 }
 return 0;
}
static int execute(const char *script,int output,unsigned timeout_ms) {
 pid_t child=fork();if(child<0)return 125;
 if(!child) {
  setpgid(0,0);
  int in=open("/dev/null",O_RDONLY);
  if(in<0||dup2(in,0)<0||dup2(output,1)<0||dup2(output,2)<0)_exit(125);
  close(in);
  execl("/bin/sh","sh","-c",script,(char *)NULL);_exit(127);
 }
 setpgid(child,child);
 int pidfd=syscall(SYS_pidfd_open,child,0),status;
 if(pidfd<0){kill(-child,SIGKILL);waitpid(child,NULL,0);return 125;}
 struct pollfd wait={.fd=pidfd,.events=POLLIN};int rc;
 do {rc=poll(&wait,1,timeout_ms);}while(rc<0&&errno==EINTR);
 close(pidfd);
 if(rc<=0){kill(-child,SIGKILL);waitpid(child,NULL,0);return 124;}
 while(waitpid(child,&status,0)<0){if(errno!=EINTR)return 125;}
 return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
int main(int argc,char **argv) {
 if(argc!=3||strlen(argv[2])>32||!strlen(argv[2])||strspn(argv[2],"0123456789abcdef")!=strlen(argv[2]))return 2;
 char nonce[32]={0};memcpy(nonce,argv[2],strlen(argv[2]));
 signal(SIGPIPE,SIG_IGN);
 int fd=open(argv[1],O_RDWR|O_CLOEXEC|O_NONBLOCK);if(fd<0)die("control port");
 int epoll=epoll_create1(EPOLL_CLOEXEC);if(epoll<0)die("control epoll");
 struct epoll_event event={.events=EPOLLIN|EPOLLET,.data.fd=fd};
 if(epoll_ctl(epoll,EPOLL_CTL_ADD,fd,&event))die("control watch");
 printf("LAB_CONTROL_READY\n");fflush(stdout);
 for(;;) {
  int ready;
  do {ready=epoll_wait(epoll,&event,1,-1);}while(ready<0&&errno==EINTR);
  if(ready<0)die("control wait");
  if(!(event.events&EPOLLIN))continue;
  struct request q;
  // virtio-serial reports readable EOF while no host is connected. With ET
  // this sleeps until the connection/data changes, without an idle poll loop.
  ssize_t first=read(fd,&q,1);
  if(first==0||(first<0&&(errno==EINTR||errno==EAGAIN)))continue;
  if(first<0||transfer(fd,(char *)&q+1,sizeof(q)-1,0,30000))die("incomplete control header");
  if(q.magic!=MAGIC||q.bytes>CAP||!q.timeout_ms||q.timeout_ms>900000)die("invalid control frame");
  char *body=malloc((size_t)q.bytes+1);if(!body)die("request allocation");
  if(transfer(fd,body,q.bytes,0,q.timeout_ms))die("incomplete control frame");
  body[q.bytes]=0;
  struct reply r={.magic=MAGIC,.status=125,.sequence=q.sequence};uint64_t started=now();
  int output=memfd_create("control-output",MFD_CLOEXEC);if(output<0)die("output");
  if(memcmp(q.nonce,nonce,sizeof(nonce))) {
   const char *message="control session mismatch\n";
   if(transfer(output,(void *)message,strlen(message),1,q.timeout_ms))die("error output");
  } else if(q.operation==1 && !memchr(body,0,q.bytes)) {
   r.status=execute(body,output,q.timeout_ms);
  } else if(q.operation==2 && q.bytes>=8) {
   uint32_t path_bytes,script_bytes;memcpy(&path_bytes,body,4);memcpy(&script_bytes,body+4,4);
   if(path_bytes>0&&path_bytes<4096&&path_bytes<=q.bytes-8&&script_bytes<=q.bytes-8-path_bytes) {
    char path[4096];memcpy(path,body+8,path_bytes);path[path_bytes]=0;
    const char *script=body+8+path_bytes;size_t bytes=q.bytes-8-path_bytes-script_bytes;
    if(!memchr(path,0,path_bytes)&&(!strncmp(path,"/run/",5)||!strncmp(path,"/tmp/",5))&&
       !memchr(script,0,script_bytes)) {
     int file=open(path,O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0600);
     if(file>=0) {
      int result=transfer(file,(void *)(script+script_bytes),bytes,1,q.timeout_ms);
      int closed=close(file);r.status=result||closed?125:0;
      if(!r.status&&script_bytes) {
       char *command=strndup(script,script_bytes);
       r.status=command?execute(command,output,q.timeout_ms):125;free(command);
      }
     }
    }
   }
  }
  free(body);r.elapsed_ns=now()-started;
  off_t size=lseek(output,0,SEEK_END);if(size<0||size>CAP)die("output bound");r.bytes=size;
  if(transfer(fd,&r,sizeof(r),1,q.timeout_ms))die("reply header");
  lseek(output,0,SEEK_SET);char buffer[65536];
  while(size) {
   ssize_t n=read(output,buffer,size<(off_t)sizeof(buffer)?(size_t)size:sizeof(buffer));
   if(n<=0||transfer(fd,buffer,n,1,q.timeout_ms))die("reply data");
   size-=n;
  }
  close(output);
 }
}
