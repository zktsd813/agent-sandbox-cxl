"""Lab commands and owned Firecracker process cleanup for the EPT fixture."""
import json
import shlex
import lab

def shell(vm, body, timeout=60):
    return lab.guest_command(vm, body, timeout=timeout)

def record_pid(directory):
    return ("p=$!\ns=$(cat /proc/$p/stat)\ns=${s##*) }\nset -- $s\nshift 19\n"
            f"printf '%s %s\\n' \"$p\" \"$1\" > {directory}/fc.pid\n")

def stop_fc(vm, directory):
    return shell(vm, lab.process_cleanup_script() +
          f"if [ -f {directory}/fc.pid ]; then\nread target_pid expected_start < {directory}/fc.pid\n"
          "case \"$target_pid:$expected_start\" in *[!0-9:]*|:|*:) exit 1 ;; esac\n"
          "[ \"$target_pid\" -gt 1 ]\nstop_worker_process\nfi\n")

def api(vm, directory, path, method, body):
    return shell(vm, f"/lab/shared-probe api {directory}/api.sock {method} {path} " + shlex.quote(json.dumps(body)))
