// SPDX-License-Identifier: Apache-2.0
// Fixed x86-64 little-endian lab protocol. Changing its layout requires a new magic.
#ifndef RESIDENT_WIRE_H
#define RESIDENT_WIRE_H
#include <stddef.h>
#include <stdint.h>
#define RESIDENT_MAGIC 0x43585233u
struct request {
 uint32_t magic, state_len, manifest_len, first, operation, observe, persistent;
 uint64_t vm, epoch, canonical_start, canonical_bytes;
 char nonce[33], label[96], peer[16];
};
struct api_result { double us; int status; };
struct reply { uint32_t magic, ack_len; struct api_result receive; uint64_t ack_ns; };
struct outcome {
 struct api_result send;
 struct reply target;
 uint64_t handoff_ns, connection_ns, pause_to_resume_ns;
 uint32_t state_len, manifest_len;
};
_Static_assert(sizeof(struct request)==216 && offsetof(struct request,vm)==32 &&
 offsetof(struct request,nonce)==64 && sizeof(struct outcome)==80 &&
 sizeof(struct reply)==32,"resident wire layout");
#endif
