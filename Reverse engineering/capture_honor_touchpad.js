// Frida 17 capture - PCManagerTray.exe
// Hooks IPC PostIPCMessage + WriteFile with proper closure + buffer reading

function readBytes(addr, len) {
    try {
        if (len <= 0 || len > 4096) return '';
        var bytes = addr.readByteArray(len);
        var arr = new Uint8Array(bytes);
        var s = [];
        for (var i = 0; i < arr.length; i++) s.push(('0' + arr[i].toString(16)).slice(-2));
        return s.join(' ');
    } catch (e) {
        return '<err:' + e.message + '>';
    }
}

function readAnsi(addr, maxLen) {
    try { return addr.readAnsiString(maxLen || 256); } catch (e) { return null; }
}

function readUtf16(addr, maxLen) {
    try { return addr.readUtf16String(maxLen || 256); } catch (e) { return null; }
}

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

function logCapture(tag, data) {
    var line = '[' + new Date().toISOString().substr(11, 12) + '] ' + tag + ' ' + data;
    console.log(line);
    send({ type: 'capture', tag: tag, data: data, timestamp: new Date().toISOString() });
}

// List relevant modules
var mods = Process.enumerateModules();
for (var i = 0; i < mods.length; i++) {
    var n = mods[i].name.toLowerCase();
    if (n.indexOf('ipc') !== -1 || n.indexOf('magic') !== -1 || n.indexOf('hid') !== -1 || n.indexOf('touch') !== -1) {
        logCapture('MODULE', mods[i].name + ' @ ' + mods[i].base + ' size=' + mods[i].size);
    }
}

// ── Hook IPC PostIPCMessage / SendIPCMessage etc ──
// Fix closure: capture name per-hook
var ipcMod = null;
for (var i = 0; i < mods.length; i++) {
    if (mods[i].name.toLowerCase() === 'ipcmessage.dll') { ipcMod = mods[i]; break; }
}
if (ipcMod) {
    var ipcExports = ipcMod.enumerateExports();
    ipcExports.forEach(function (exp) {
        var name = exp.name;
        if (name.indexOf('PostIPCMessage') !== -1 || name.indexOf('SendIPCData') !== -1 ||
            name.indexOf('SendIPCMessage') !== -1 || name.indexOf('PublishIPC') !== -1) {
            try {
                Interceptor.attach(exp.address, (function (fname, faddr) {
                    return {
                        onEnter: function (args) {
                            // PostIPCMessage(this, TagIPCMessageItem&) - args[0]=this(ptr), args[1]=msgItem ref
                            // SendIPCData(this, TagIPCMessageItem&, TagIPCMessageItem&, uint) - args
                            // Try reading TagIPCMessageItem - it's passed by reference (AEBU = const ref, AEAU = ref)
                            this.fname = fname;
                            this.args = [];
                            for (var i = 0; i < 4; i++) {
                                try { this.args.push(args[i]); } catch (e) { this.args.push(ptr(0)); }
                            }
                            // Try to read the IPC message item as a struct
                            // TagIPCMessageItem likely has: int moduleId, int msgId, string data, int dataLen, etc.
                            // Read raw bytes to see the structure
                            // Try both arg[0] and arg[1] as potential message pointers
                            for (var ai = 0; ai < 3; ai++) {
                                try {
                                    var raw = readBytes(this.args[ai], 128);
                                    if (raw && raw.indexOf('<err') === -1) {
                                        this.msgRaw = 'arg[' + ai + ']=' + this.args[ai] + ' bytes=' + raw;
                                        break;
                                    }
                                } catch (e) {}
                            }
                        },
                        onLeave: function (retval) {
                            logCapture('IPC', this.fname + ' ret=' + retval +
                                (this.msgRaw ? ' MSG=' + this.msgRaw : ''));
                        }
                    };
                })(name, exp.address));
                logCapture('HOOK', name + ' @ ' + exp.address);
            } catch (e) { logCapture('ERR', 'hook ' + name + ': ' + e); }
        }
    });
}

// ── Hook WriteFile - read buffer in onEnter (before completion) ──
var writeFn = findExport('WriteFile');
if (writeFn) {
    Interceptor.attach(writeFn, {
        onEnter: function (args) {
            this.len = args[2].toInt32();
            if (this.len > 4096) { this.skip = true; return; }
            this.h = args[0];
            // Read the buffer NOW in onEnter while it's definitely valid
            this.data = readBytes(args[1], this.len);
            // Try to extract ASCII strings from the buffer
            try {
                var buf = args[1].readByteArray(this.len);
                var arr = new Uint8Array(buf);
                var strs = [];
                var cur = '';
                for (var i = 0; i < arr.length; i++) {
                    if (arr[i] >= 0x20 && arr[i] < 0x7f) { cur += String.fromCharCode(arr[i]); }
                    else { if (cur.length >= 4) strs.push(cur); cur = ''; }
                }
                if (cur.length >= 4) strs.push(cur);
                this.strings = strs.join(' | ');
            } catch (e) { this.strings = ''; }
        },
        onLeave: function (retval) {
            if (this.skip) return;
            logCapture('WriteFile', 'h=' + this.h + ' len=' + this.len + ' ret=' + retval +
                ' hex=' + this.data +
                (this.strings ? ' strings=[' + this.strings + ']' : ''));
        }
    });
    logCapture('HOOK', 'WriteFile');
}

// ── Hook ReadFile ──
var readFn = findExport('ReadFile');
if (readFn) {
    Interceptor.attach(readFn, {
        onEnter: function (args) {
            this.len = args[2].toInt32();
            if (this.len > 4096) { this.skip = true; return; }
            this.h = args[0];
            this.buf = args[1];
        },
        onLeave: function (retval) {
            if (this.skip) return;
            var data = readBytes(this.buf, this.len);
            logCapture('ReadFile', 'h=' + this.h + ' len=' + this.len + ' ret=' + retval + ' hex=' + data);
        }
    });
    logCapture('HOOK', 'ReadFile');
}

// ── Hook CreateFileA/W for HID device opens ──
function hookCreateFile(name) {
    var fn = findExport(name);
    if (!fn) return;
    Interceptor.attach(fn, (function (fname) {
        return {
            onEnter: function (args) {
                try {
                    if (fname === 'CreateFileW') this.path = readUtf16(args[0]);
                    else this.path = readAnsi(args[0]);
                } catch (e) { this.path = ''; }
            },
            onLeave: function (retval) {
                if (!this.path) return;
                var p = this.path.toLowerCase();
                if (p.indexOf('hid#') !== -1 || p.indexOf('tops') !== -1 || p.indexOf('pipe') !== -1 && p.indexOf('pcmanager') !== -1) {
                    logCapture('CreateFile', 'OPEN ' + (this.path) + ' h=' + retval);
                }
            }
        };
    })(name));
    logCapture('HOOK', name);
}
hookCreateFile('CreateFileA');
hookCreateFile('CreateFileW');

// ── Hook GetProcAddress for dynamic HidD_* resolution ──
var gpa = findExport('GetProcAddress');
if (gpa) {
    Interceptor.attach(gpa, {
        onEnter: function (args) {
            try { this.name = readAnsi(args[1]); } catch (e) { this.name = ''; }
        },
        onLeave: function (retval) {
            if (!this.name || retval.isNull()) return;
            if (this.name.indexOf('HidD_') === 0 || this.name.indexOf('HidP_') === 0) {
                logCapture('GetProcAddress', this.name + ' -> ' + retval);
            }
        }
    });
    logCapture('HOOK', 'GetProcAddress');
}

// Also try to hook MagicTouchPadHelper if it is or gets loaded
function hookHelperExports() {
    var helperMod = null;
    var allMods = Process.enumerateModules();
    for (var i = 0; i < allMods.length; i++) {
        if (allMods[i].name.toLowerCase() === 'magictouchpadhelper.dll') { helperMod = allMods[i]; break; }
    }
    if (helperMod) {
        logCapture('INFO', 'MagicTouchPadHelper.dll found at ' + helperMod.base);
        var exps = helperMod.enumerateExports();
        exps.forEach(function (exp) {
            var name = exp.name;
            if (name.indexOf('Change') === 0 || name.indexOf('Register') === 0) {
                try {
                    Interceptor.attach(exp.address, (function (fname) {
                        return {
                            onEnter: function (args) {
                                logCapture('HELPER', fname + ' called arg0=' + args[0] + ' arg1=' + args[1] + ' arg2=' + args[2]);
                            }
                        };
                    })(name));
                    logCapture('HOOK', 'Helper: ' + name);
                } catch (e) {}
            }
        });
    }
}
hookHelperExports();

// Re-check for Helper DLL periodically (it might load lazily)
var checkCount = 0;
var helperInterval = setInterval(function () {
    checkCount++;
    if (checkCount > 60) { clearInterval(helperInterval); return; }
    var allMods = Process.enumerateModules();
    for (var i = 0; i < allMods.length; i++) {
        if (allMods[i].name.toLowerCase() === 'magictouchpadhelper.dll') {
            logCapture('INFO', 'MagicTouchPadHelper.dll loaded at ' + allMods[i].base + '!');
            hookHelperExports();
            clearInterval(helperInterval);
            return;
        }
    }
}, 2000);

logCapture('READY', 'Tray capture running. Toggle trackpad settings now.');