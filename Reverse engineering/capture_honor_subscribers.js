// Frida JS payload — runs inside each Honor subscriber process.
// Tag is set via Python by string-substituting '<TAG>' before loading.
'use strict';

function ts() {
    var d = new Date();
    function pad(n){ return ('0'+n).slice(-2); }
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + '.' +
           ('00'+d.getMilliseconds()).slice(-3);
}
function hex(buf, len, max) {
    if (!buf) return '(null)';
    try {
        if (len <= 0) return '(empty)';
        if (len > 2048) len = 2048;     // safety cap
        var ba = new Uint8Array(buf.readByteArray(len));
        var real = ba.length;
        if (max && real > max) real = max;
        var s = '';
        for (var i = 0; i < real; i++) { var b = ba[i]; s += (b < 16 ? '0' : '') + b.toString(16) + ' '; }
        if (ba.length > real) s += '...(' + ba.length + 'B)';
        return s.trim();
    } catch (e) { return 'hex_err:' + e; }
}
function ascii(buf, len, max) {
    if (!buf) return '';
    try {
        if (len <= 0) return '';
        if (len > 2048) len = 2048;
        var ba = new Uint8Array(buf.readByteArray(len));
        var real = ba.length;
        if (max && real > max) real = max;
        var s = '';
        for (var i = 0; i < real; i++) { var b = ba[i]; if (b>=32 && b<127) s += String.fromCharCode(b); }
        return s;
    } catch (e) { return ''; }
}
function send_log(msg) { send('[' + ts() + '] ' + TAG + ' | ' + msg); }

// Frida 17 helper - the old Module.findExportByName(modName, exportName) no longer exists.
function findExport(name) {
    try { var fn = Module.findGlobalExportByName(name); if (fn) return fn; } catch (e) {}
    try {
        var mods = Process.enumerateModules();
        for (var i = 0; i < mods.length; i++) {
            try { var exp = mods[i].findExportByName(name); if (exp) return exp; } catch (e) {}
        }
    } catch (e) {}
    return null;
}

// ----- Dump loaded modules -----
(function () {
    var mods = Process.enumerateModules();
    var keyRex = /hid|hnsdk|trifinger|magic|touch|vhid|wmi|setupapi|cfgmgr|acpi|rpcrt|honor|plugin/i;
    send_log('=== MODULES (total ' + mods.length + ') ===');
    for (var i = 0; i < mods.length && i < 80; i++) {
        send_log('MOD ' + mods[i].name + ' @ ' + mods[i].base + ' size=' + mods[i].size);
    }
    send_log('=== KEY MODULES ===');
    for (var i = 0; i < mods.length; i++) {
        if (keyRex.test(mods[i].name)) send_log('KEY ' + mods[i].name + ' @ ' + mods[i].base);
    }
})();

// ----- CreateFileW: see what device / pipe / file handles get opened -----
(function () {
    var p = findExport('CreateFileW');
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (args) {
            var path = args[0].readUtf16String();
            if (!path) return;
            var lp = path.toLowerCase();
            if (lp.indexOf('hid') >= 0 || lp.indexOf('acpi') >= 0 || lp.indexOf('wmi') >= 0 ||
                lp.indexOf('\\\\.\\') === 0 || lp.indexOf('\\\\?\\') === 0 || lp.indexOf('pipe') >= 0) {
                send_log('CreateFileW ' + path);
                this.log = true;
            }
        },
        onLeave: function (ret) { if (this.log) send_log('  -> ret=0x' + ret.toString(16)); },
    });
    send_log('HOOK CreateFileW');
})();

// ----- LoadLibrary: detect newly-loaded HID/WMI libs -----
['LoadLibraryW', 'LoadLibraryExW'].forEach(function (fn) {
    var p = findExport(fn);
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var n = a[0].readUtf16String();
            if (n) {
                var l = n.toLowerCase();
                if (l.indexOf('hid') >= 0 || l.indexOf('magic') >= 0 || l.indexOf('touch') >= 0 ||
                    l.indexOf('trifinger') >= 0 || l.indexOf('vhid') >= 0 || l.indexOf('wmi') >= 0 ||
                    l.indexOf('hnsdk') >= 0) {
                    send_log(fn + ' ' + n);
                    this.log = true;
                }
            }
        },
        onLeave: function (r) { if (this.log) send_log('  -> ret=0x' + r.toString(16)); },
    });
    send_log('HOOK ' + fn);
});

// ----- NtDeviceIoControlFile: catch all IOCTLs to HID (0xB0xxx), ACPI, WMI -----
(function () {
    var p = findExport('NtDeviceIoControlFile');
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var h = a[0]; var ioctl = a[5].toInt32 ? a[5].toInt32() : parseInt(a[5]);
            var inBuf = a[6]; var inLen = a[7].toInt32 ? a[7].toInt32() : parseInt(a[7]);
            var outBuf = a[8]; var outLen = a[9].toInt32 ? a[9].toInt32() : parseInt(a[9]);
            if (inLen < 0) inLen += 0x100000000;
            if (outLen < 0) outLen += 0x100000000;
            var code = ioctl; if (code < 0) code += 0x100000000;
            var hexCode = '0x' + code.toString(16);
            this.tag = null;
            var inHex = '';
            if (inBuf && !inBuf.isNull() && inLen > 0 && inLen < 512) {
                inHex = hex(inBuf, inLen > 128 ? 128 : inLen);
            }
            var wmi_sniff = inHex.indexOf('c0 b0 ea f9 d4 26 d0 11 bb bf 00 aa 00 6c 34') >= 0;
            // 0xB0xxx HID, 0x222xxx generic device method, small buffer (HID feature id)
            if ((code >= 0xb0000 && code < 0xb1000) || wmi_sniff ||
                (code >= 0x222000 && code < 0x223000) || code === 0x12047 || code === 0x120bf ||
                (inLen >= 1 && inLen <= 16 && inBuf && !inBuf.isNull())) {
                this.tag = hexCode + ' h=' + h + ' inLen=' + inLen + ' outLen=' + outLen + ' IN=' + inHex;
                send_log('NtIoctl ' + this.tag);
                this.outBuf = outBuf; this.outLen = outLen; this.outCode = code;
            }
        },
        onLeave: function (r) {
            if (this.tag && this.outLen > 0 && this.outBuf && !this.outBuf.isNull()) {
                try {
                    send_log('NtIoctl OUT ' + hex(this.outBuf, this.outLen, 128) +
                             ' ret=0x' + r.toString(16));
                } catch (e) {}
            }
        },
    });
    send_log('HOOK NtDeviceIoControlFile');
})();

// ----- DeviceIoControl (kernel32 user-mode wrapper, may not be inlined) -----
(function () {
    var p = findExport('DeviceIoControl');
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var h = a[0]; var ioctl = a[1].toInt32 ? a[1].toInt32() : parseInt(a[1]);
            var inBuf = a[2]; var inLen = a[3].toInt32 ? a[3].toInt32() : parseInt(a[3]);
            var outBuf = a[4]; var outLen = a[5].toInt32 ? a[5].toInt32() : parseInt(a[5]);
            if (inLen < 0) inLen += 0x100000000;
            if (outLen < 0) outLen += 0x100000000;
            if (ioctl < 0) ioctl += 0x100000000;
            var hexCode = '0x' + ioctl.toString(16);
            var inHex = '';
            if (inBuf && !inBuf.isNull() && inLen > 0 && inLen < 256) {
                inHex = hex(inBuf, inLen, 128);
            }
            // Filter to interesting IOCTLs: HID (0xB0xxx), generic device methods (0x222xxx), small buffers (likely feature reports)
            if ((ioctl >= 0xb0000 && ioctl < 0xb1000) ||
                (ioctl >= 0x222000 && ioctl < 0x223000) ||
                inLen <= 32) {
                send_log('DevIoCtl h=' + h + ' IOCTL=' + hexCode + ' inLen=' + inLen + ' outLen=' + outLen + ' IN=' + inHex);
                this.outBuf = outBuf; this.outLen = outLen; this.log = true;
            }
        },
        onLeave: function (r) {
            if (this.log && this.outLen > 0 && this.outBuf && !this.outBuf.isNull()) {
                try { send_log('DevIoCtl OUT ' + hex(this.outBuf, this.outLen, 128) + ' ret=' + r); } catch (e) {}
            }
        },
    });
    send_log('HOOK DeviceIoControl');
})();

// ----- NtFsControlFile: alternative device I/O -----
(function () {
    var p = findExport('NtFsControlFile');
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var h = a[0]; var fsctl = a[5].toInt32 ? a[5].toInt32() : parseInt(a[5]);
            var inBuf = a[6]; var inLen = a[7].toInt32 ? a[7].toInt32() : parseInt(a[7]);
            if (inLen < 0) inLen += 0x100000000;
            if (fsctl < 0) fsctl += 0x100000000;
            var hexCode = '0x' + fsctl.toString(16);
            if (inLen > 0 && inLen < 256) {
                send_log('NtFsCtl h=' + h + ' FsCtl=' + hexCode + ' inLen=' + inLen + ' IN=' + hex(inBuf, inLen, 128));
            }
        },
    });
    send_log('HOOK NtFsControlFile');
})();

// ----- NtWriteFile: any write to HID device handle or pipe -----
(function () {
    var p = findExport('NtWriteFile');
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var h = a[0]; var buf = a[5]; var len = a[6].toInt32 ? a[6].toInt32() : parseInt(a[6]);
            if (len < 0) len += 0x100000000;
            if (len > 0 && len < 2048) {
                var hx = hex(buf, len, 256);
                var ao = ascii(buf, len, 96);
                send_log('NtWrite h=' + h + ' len=' + len + ' hex=' + hx + ' ascii=[' + ao + ']');
            } else if (len >= 2048) {
                send_log('NtWrite h=' + h + ' len=' + len + ' big');
            }
        },
    });
    send_log('HOOK NtWriteFile');
})();

// ----- NtReadFile: just log small reads (HID input reports) -----
(function () {
    var p = findExport('NtReadFile');
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var h = a[0]; var buf = a[5]; var len = a[6].toInt32 ? a[6].toInt32() : parseInt(a[6]);
            if (len < 0) len += 0x100000000;
            if (len > 0 && len < 1024) {
                send_log('NtRead h=' + h + ' len=' + len);
            }
        },
    });
    send_log('HOOK NtReadFile');
})();

// ----- Registry writes -----
['RegSetValueExW', 'RegSetValueExA', 'RegCreateKeyExW', 'RegCreateKeyExA'].forEach(function (fn) {
    var p = findExport(fn);
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            try {
                var s = null;
                for (var i = 0; i < 6; i++) {
                    if (a[i] && !a[i].isNull()) {
                        var v = a[i].readUtf16String && a[i].readUtf16String();
                        if (v && v.length > 3) { s = v; break; }
                    }
                }
                send_log(fn + ' ' + (s || ''));
                this.log = true;
            } catch (e) {}
        },
        onLeave: function (r) { if (this.log) send_log('  -> ret=0x' + r.toString(16)); },
    });
    send_log('HOOK ' + fn);
});

// ----- Direct HID APIs from hid.dll -----
['HidD_SetFeature', 'HidD_SetOutputReport', 'HidD_GetFeature', 'HidD_GetInputReport',
 'HidD_GetAttributes', 'HidD_GetManufacturerString', 'HidD_GetProductString',
 'HidD_GetSerialNumberString'].forEach(function (fn) {
    var p = findExport(fn);
    if (!p) return;
    Interceptor.attach(p, {
        onEnter: function (a) {
            var h = a[0]; var buf = a[1]; var len = parseInt(a[2]);
            var hx = (buf && !buf.isNull()) ? hex(buf, len, 32) : '(null)';
            send_log(fn + ' h=' + h + ' len=' + len + ' data=' + hx);
        },
        onLeave: function (r) { send_log('  -> ret=0x' + r.toString(16)); },
    });
    send_log('HOOK ' + fn);
});

send_log('READY');
