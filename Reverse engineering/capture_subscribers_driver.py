#!/usr/bin/env python3
"""Driver: attach Frida to HnPerfPowerNexus, HnPCAIService x2, HnPerformanceCenter,
hook all HID/WMI/registry/syscall APIs, route logs to one console + one file.
Wait for user to toggle a touchpad setting in MagicTouchPadSettingUI."""
import sys, time, frida

LOG = open(r'C:\Users\zacha\AppData\Local\Temp\opencode\frida_subscribers.log',
           'a', encoding='utf-8')
def L(s):
    print(s); LOG.write(s + '\n'); LOG.flush()

TARGETS = [
    ('ui',         15824, 'MagicTouchPadSettingUI'),
    ('pcmgr',       5580, 'PCManager'),
    ('main_svc',   36500, 'PCManagerMainService'),
    ('perf_power',  3360, 'HnPerfPowerNexus'),
    ('pcais_1',   19736, 'HnPCAIService_1'),
    ('pcais_2',   29232, 'HnPCAIService_2'),
    ('perf_ctr',  41868, 'HnPerformanceCenter'),
]

with open(r'C:\Users\zacha\Desktop\capture_honor_subscribers.js', 'r', encoding='utf-8') as f:
    js_template = f.read()

sessions = []
def attach(key, pid, name):
    try:
        s = frida.attach(pid)
        js = "var TAG = '" + name + "';\n" + js_template
        sc = s.create_script(js)
        def on_msg(m, d, k=key):
            if m.get('type') == 'send':
                L(m.get('payload', ''))
            elif m.get('type') == 'error':
                L('[' + k + ' ERROR] ' + m.get('description','') + '\n' + m.get('stack',''))
        sc.on('message', on_msg)
        sc.load()
        sessions.append(s)
        L('attached ' + name + ' pid=' + str(pid))
    except Exception as e:
        L('FAILED ' + name + ' pid=' + str(pid) + ': ' + str(e))

L('\n==== new session ' + time.strftime('%H:%M:%S') + ' ====')
for k, p, n in TARGETS:
    attach(k, p, n)
L('All attached. Toggle ALL remaining touchpad settings now. Will run for 10 minutes (600s).')
L('Ctrl-C to stop earlier.')
try:
    for _ in range(600):
        time.sleep(1)
except KeyboardInterrupt:
    pass
