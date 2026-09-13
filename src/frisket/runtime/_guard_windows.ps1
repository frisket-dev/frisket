param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'

# The only Windows-specific ownership boundary. Create the target suspended so
# no application code can spawn descendants before it belongs to our Job.
# Neither PowerShell nor C# reads the inherited target protocol streams.
try {
    $launch = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class FrisketWindowsGuard {
    [StructLayout(LayoutKind.Sequential)] struct Startup {
        public uint cb; public IntPtr reserved, desktop, title;
        public uint x, y, width, height, xChars, yChars, fill, flags;
        public ushort show, reservedSize; public IntPtr reservedBytes, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] struct ProcessInfo {
        public IntPtr process, thread; public uint pid, tid;
    }
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public long processTime, jobTime; public uint flags;
        public UIntPtr minWorking, maxWorking; public uint activeLimit;
        public UIntPtr affinity; public uint priority, scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters {
        public ulong readOps, writeOps, otherOps, readBytes, writeBytes, otherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public Limits basic; public IoCounters io;
        public UIntPtr processMemory, jobMemory, peakProcessMemory, peakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long user, kernel, periodUser, periodKernel;
        public uint faults, total, active, terminated;
    }
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits limits, uint length);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job, int kind, out Accounting info, uint length, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern bool CreateProcessW(string application, StringBuilder command, IntPtr processAttributes, IntPtr threadAttributes, bool inherit, uint flags, IntPtr environment, string directory, ref Startup startup, out ProcessInfo info);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int kind);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);

    static void Check(bool ok) { if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    static string Quote(string value) {
        // CommandLineToArgvW/CRT quoting: double backslashes before a quote or
        // closing delimiter. Always quote, including empty arguments.
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(c); slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }
    static bool Exited(IntPtr process, uint timeout) {
        uint state = WaitForSingleObject(process, timeout);
        if (state == 0xffffffff) throw new Win32Exception(Marshal.GetLastWin32Error());
        return state == 0;
    }
    static uint Active(IntPtr job) {
        Accounting info;
        Check(QueryInformationJobObject(job, 1, out info, (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero));
        return info.active;
    }
    public static int Run(string command, string[] args, uint parentPid, uint ownerPid, string stop, string proof) {
        IntPtr parent = OpenProcess(0x00100000, false, parentPid);
        if (parent == IntPtr.Zero) {
            // An already-dead parent cannot authorize a late target spawn.
            if (Marshal.GetLastWin32Error() != 87) throw new Win32Exception(Marshal.GetLastWin32Error());
            File.WriteAllText(proof, "clean"); return 0;
        }
        IntPtr owner = IntPtr.Zero;
        IntPtr job = IntPtr.Zero; ProcessInfo child = new ProcessInfo();
        try {
            if (ownerPid != 0 && ownerPid != parentPid) {
                owner = OpenProcess(0x00100000, false, ownerPid);
                if (owner == IntPtr.Zero) {
                    if (Marshal.GetLastWin32Error() != 87) throw new Win32Exception(Marshal.GetLastWin32Error());
                    File.WriteAllText(proof, "clean"); return 0;
                }
            }
            if (Exited(parent, 0) || (owner != IntPtr.Zero && Exited(owner, 0)) || File.Exists(stop)) {
                File.WriteAllText(proof, "clean"); return 0;
            }
            job = CreateJobObjectW(IntPtr.Zero, null);
            Check(job != IntPtr.Zero);
            var limits = new ExtendedLimits(); limits.basic.flags = 0x2000; // KILL_ON_JOB_CLOSE
            Check(SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits))));
            var startup = new Startup(); startup.cb = (uint)Marshal.SizeOf(typeof(Startup));
            startup.flags = 0x100; // STARTF_USESTDHANDLES; keep stdin/stdout byte-transparent
            startup.input = GetStdHandle(-10); startup.output = GetStdHandle(-11); startup.error = GetStdHandle(-12);
            var line = new StringBuilder(Quote(command));
            foreach (string arg in args) line.Append(" ").Append(Quote(arg));
            // CREATE_SUSPENDED | CREATE_NO_WINDOW: a headless guardian does
            // not make its console-subsystem children headless automatically.
            Check(CreateProcessW(command, line, IntPtr.Zero, IntPtr.Zero, true, 4 | 0x08000000, IntPtr.Zero, null, ref startup, out child));
            // Assignment failure leaves a suspended process. Terminate it in
            // finally; never let the target run outside the owned Job.
            Check(AssignProcessToJobObject(job, child.process));
            if (!Exited(parent, 0) && (owner == IntPtr.Zero || !Exited(owner, 0)) && !File.Exists(stop)) {
                Check(ResumeThread(child.thread) != 0xffffffff);
            }
            uint code = 0;
            while (!Exited(child.process, 25)) {
                if (Exited(parent, 0) || (owner != IntPtr.Zero && Exited(owner, 0)) || File.Exists(stop)) break;
            }
            if (Exited(child.process, 0)) Check(GetExitCodeProcess(child.process, out code));
            Check(TerminateJobObject(job, 1));
            DateTime deadline = DateTime.UtcNow.AddSeconds(8);
            while (Active(job) != 0) {
                if (DateTime.UtcNow > deadline) throw new TimeoutException("Windows runtime cleanup did not complete.");
                Thread.Sleep(10);
            }
            if (Exited(parent, 0)) {
                // Our JS owner cannot retire its small control directory after
                // an abrupt exit. All target processes are gone at this point.
                Directory.Delete(Path.GetDirectoryName(proof), true);
            } else File.WriteAllText(proof, "clean");
            return unchecked((int)code);
        } finally {
            if (child.process != IntPtr.Zero && job != IntPtr.Zero) {
                // TerminateProcess also covers the suspended assignment-failure case.
                TerminateProcess(child.process, 1);
            }
            if (job != IntPtr.Zero) CloseHandle(job);
            if (child.thread != IntPtr.Zero) CloseHandle(child.thread);
            if (child.process != IntPtr.Zero) CloseHandle(child.process);
            CloseHandle(parent);
            if (owner != IntPtr.Zero) CloseHandle(owner);
        }
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process, uint code);
}
'@
    if (![System.IO.Path]::IsPathRooted($launch.command) -or $launch.parentPid -lt 1 -or
        ![System.IO.Path]::IsPathRooted($launch.stop) -or ![System.IO.Path]::IsPathRooted($launch.proof)) {
        throw 'invalid Windows runtime configuration'
    }
    exit [FrisketWindowsGuard]::Run([string]$launch.command, [string[]]$launch.args,
        [uint32]$launch.parentPid, [uint32]$launch.ownerPid, [string]$launch.stop, [string]$launch.proof)
} catch {
    [Console]::Error.WriteLine('Windows runtime guardian failed: ' + $_.Exception.Message)
    exit 125
}
