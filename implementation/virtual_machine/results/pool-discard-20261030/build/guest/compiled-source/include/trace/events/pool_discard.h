/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM pool_discard
#if !defined(_TRACE_POOL_DISCARD_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_POOL_DISCARD_H
#include <linux/tracepoint.h>
/* Validation-only events: no data or allocator behavior is changed. */
TRACE_EVENT(pool_free_report,
 TP_PROTO(u64 seq, unsigned int phase, unsigned int index, unsigned int nents,
          u64 gpa, u64 bytes, int result),
 TP_ARGS(seq, phase, index, nents, gpa, bytes, result),
 TP_STRUCT__entry(
  __field(u64, seq)
  __field(unsigned int, phase)
  __field(unsigned int, index)
  __field(unsigned int, nents)
  __field(u64, gpa)
  __field(u64, bytes)
  __field(int, result)
 ),
 TP_fast_assign(
  __entry->seq = seq;
  __entry->phase = phase;
  __entry->index = index;
  __entry->nents = nents;
  __entry->gpa = gpa;
  __entry->bytes = bytes;
  __entry->result = result;
 ),
 TP_printk("seq=%llu phase=%u index=%u nents=%u gpa=%llu bytes=%llu result=%d",
           __entry->seq, __entry->phase, __entry->index, __entry->nents,
           __entry->gpa, __entry->bytes, __entry->result)
);
#endif
#include <trace/define_trace.h>
