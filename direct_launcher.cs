using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32;

internal static class Program
{
    private const uint CREATE_SUSPENDED = 0x00000004;
    private const uint DETACHED_PROCESS = 0x00000008;
    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    private const uint MEM_COMMIT = 0x1000;
    private const uint MEM_RESERVE = 0x2000;
    private const uint MEM_RELEASE = 0x8000;
    private const uint PAGE_READWRITE = 0x04;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 0x102;
    private const uint INFINITE = 0xFFFFFFFF;
    private const uint MB_ICONERROR = 0x10;
    private const uint MB_OK = 0;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public uint cb;
        public IntPtr lpReserved;
        public IntPtr lpDesktop;
        public IntPtr lpTitle;
        public uint dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public ushort wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public uint dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateProcessW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(
        string applicationName, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        uint creationFlags, IntPtr environment, string currentDirectory,
        ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr VirtualAllocEx(IntPtr process, IntPtr address, UIntPtr size,
        uint allocationType, uint protect);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool VirtualFreeEx(IntPtr process, IntPtr address, UIntPtr size, uint freeType);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool WriteProcessMemory(IntPtr process, IntPtr address, byte[] buffer,
        UIntPtr size, out UIntPtr bytesWritten);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr CreateRemoteThread(IntPtr process, IntPtr threadAttributes,
        UIntPtr stackSize, IntPtr startAddress, IntPtr parameter, uint creationFlags, out uint threadId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeThread(IntPtr thread, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetModuleHandleA(string moduleName);

    [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string procName);

    [DllImport("user32.dll", EntryPoint = "MessageBoxW", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr owner, string text, string caption, uint type);

    private static string LogPath
    {
        get
        {
            string basePath = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (String.IsNullOrEmpty(basePath)) basePath = Path.GetTempPath();
            return Path.Combine(basePath, "LEDERG CRMP", "direct-launch.log");
        }
    }

    private static void Log(string message)
    {
        try
        {
            string path = LogPath;
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.AppendAllText(path, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
                + " " + message + Environment.NewLine, Encoding.UTF8);
        }
        catch { }
    }

    private static Exception Win32Error(string operation)
    {
        return new InvalidOperationException(operation + " (Win32 error " + Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture) + ").");
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static IntPtr BuildEnvironment(string dllDirectory)
    {
        IDictionary environment = Environment.GetEnvironmentVariables();
        List<string> entries = new List<string>();
        string oldPath = "";
        foreach (DictionaryEntry item in environment)
        {
            string key = Convert.ToString(item.Key, CultureInfo.InvariantCulture) ?? "";
            string value = Convert.ToString(item.Value, CultureInfo.InvariantCulture) ?? "";
            if (String.Equals(key, "PATH", StringComparison.OrdinalIgnoreCase))
            {
                oldPath = value;
                continue;
            }
            if (!String.IsNullOrEmpty(key)) entries.Add(key + "=" + value);
        }
        entries.Add("PATH=" + dllDirectory + ";" + oldPath);
        entries.Sort(StringComparer.OrdinalIgnoreCase);
        string block = String.Join("\0", entries.ToArray()) + "\0\0";
        return Marshal.StringToHGlobalUni(block);
    }

    private static Dictionary<string, string> ParseArguments(string[] args)
    {
        Dictionary<string, string> parsed = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (int i = 0; i + 1 < args.Length; i += 2)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal) || parsed.ContainsKey(args[i]))
                throw new ArgumentException("Некорректные параметры запуска.");
            parsed.Add(args[i], args[i + 1]);
        }
        string[] required = { "--game", "--dll", "--host", "--port", "--nickname" };
        foreach (string key in required)
            if (!parsed.ContainsKey(key) || String.IsNullOrWhiteSpace(parsed[key]))
                throw new ArgumentException("Отсутствует параметр " + key + ".");
        return parsed;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        PROCESS_INFORMATION pi = new PROCESS_INFORMATION();
        bool processCreated = false;
        bool resumed = false;
        IntPtr envPtr = IntPtr.Zero;
        try
        {
            Dictionary<string, string> options = ParseArguments(args);
            string game = Path.GetFullPath(options["--game"]);
            string dll = Path.GetFullPath(options["--dll"]);
            string host = options["--host"].Trim();
            int port;
            if (!Int32.TryParse(options["--port"], NumberStyles.None, CultureInfo.InvariantCulture, out port)
                || port < 1 || port > 65535)
                throw new ArgumentException("Порт сервера должен быть от 1 до 65535.");
            if (host.Length == 0 || host.IndexOfAny(new char[] { ' ', '\t', '\r', '\n', '"', '\\', '/' , ':' }) >= 0)
                throw new ArgumentException("Некорректный адрес сервера.");
            string nickname = options["--nickname"].Trim();
            StringBuilder cleanNick = new StringBuilder();
            foreach (char c in nickname)
                if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                    || (c >= '0' && c <= '9') || (c >= '\u0410' && c <= '\u044F')
                    || c == '\u0401' || c == '\u0451' || c == '_')
                    if (cleanNick.Length < 24) cleanNick.Append(c);
            if (cleanNick.Length == 0) cleanNick.Append("LEDERG");
            nickname = cleanNick.ToString();

            if (!File.Exists(game)) throw new FileNotFoundException("Не найден gta_sa.exe.", game);
            if (!File.Exists(dll)) throw new FileNotFoundException("Не найдена multiplayer DLL.", dll);
            string gameDirectory = Path.GetDirectoryName(game);
            string dllDirectory = Path.GetDirectoryName(dll);
            if (String.Equals(Path.GetFileName(gameDirectory), "multiplayer_c", StringComparison.OrdinalIgnoreCase))
            {
                DirectoryInfo parent = Directory.GetParent(gameDirectory);
                if (parent != null) gameDirectory = parent.FullName;
            }

            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(@"Software\SAMP"))
            {
                if (key != null) key.SetValue("PlayerName", nickname, RegistryValueKind.String);
            }

            // The multiplayer DLL reads the same native SA-MP-style startup flags
            // as the direct runtime contract: -c -n <nick> -h <host> -p <port>.
            // Passing only "host:port" opens GTA:SA without requesting a connection.
            string command = Quote(game) + " -c -n " + Quote(nickname)
                + " -h " + host + " -p " + port.ToString(CultureInfo.InvariantCulture);
            STARTUPINFO si = new STARTUPINFO();
            si.cb = (uint)Marshal.SizeOf(typeof(STARTUPINFO));
            envPtr = BuildEnvironment(dllDirectory);
            Log("Starting direct connect to " + host + ":" + port.ToString(CultureInfo.InvariantCulture)
                + " using " + Path.GetFileName(dll) + ".");

            if (!CreateProcess(game, new StringBuilder(command), IntPtr.Zero, IntPtr.Zero, false,
                DETACHED_PROCESS | CREATE_UNICODE_ENVIRONMENT,
                envPtr, gameDirectory, ref si, out pi))
                throw Win32Error("Не удалось создать игровой процесс");

            processCreated = true;
            // Let the game's initial loader and window thread run before LoadLibrary.
            // Some CR:MP builds block inside DLL startup while the primary thread is suspended.
            Thread.Sleep(650);
            if (WaitForSingleObject(pi.hProcess, 0) == WAIT_OBJECT_0)
                throw new InvalidOperationException("Игровой клиент закрылся до загрузки мультиплеера.");
            IntPtr kernel = GetModuleHandleA("kernel32.dll");
            IntPtr loadLibrary = kernel == IntPtr.Zero ? IntPtr.Zero : GetProcAddress(kernel, "LoadLibraryW");
            if (loadLibrary == IntPtr.Zero) throw Win32Error("Не удалось получить адрес LoadLibraryW");

            byte[] dllPath = Encoding.Unicode.GetBytes(dll + "\0");
            IntPtr remoteBuffer = VirtualAllocEx(pi.hProcess, IntPtr.Zero, new UIntPtr((uint)dllPath.Length),
                MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
            if (remoteBuffer == IntPtr.Zero) throw Win32Error("Не удалось выделить память в игровом процессе");

            try
            {
                UIntPtr bytesWritten;
                if (!WriteProcessMemory(pi.hProcess, remoteBuffer, dllPath, new UIntPtr((uint)dllPath.Length), out bytesWritten)
                    || bytesWritten.ToUInt64() != (ulong)dllPath.Length)
                    throw Win32Error("Не удалось передать путь multiplayer DLL в игровой процесс");

                uint remoteThreadId;
                IntPtr remoteThread = CreateRemoteThread(pi.hProcess, IntPtr.Zero, UIntPtr.Zero,
                    loadLibrary, remoteBuffer, 0, out remoteThreadId);
                if (remoteThread == IntPtr.Zero) throw Win32Error("Не удалось загрузить multiplayer DLL");

                try
                {
                    uint wait = WaitForSingleObject(remoteThread, 20000);
                    if (wait == WAIT_TIMEOUT) throw new TimeoutException("Истекло время ожидания загрузки multiplayer DLL.");
                    if (wait != WAIT_OBJECT_0) throw Win32Error("Ошибка ожидания загрузки multiplayer DLL");
                    uint module;
                    if (!GetExitCodeThread(remoteThread, out module)) throw Win32Error("Не удалось проверить загрузку multiplayer DLL");
                    if (module == 0)
                        throw new InvalidOperationException(
                            "Windows не смогла загрузить san_andreas_multiplayer.dll или одну из её зависимостей. "
                            + "Проверь комплект клиента и файлы в multiplayer_c.");
                }
                finally { CloseHandle(remoteThread); }
            }
            finally
            {
                VirtualFreeEx(pi.hProcess, remoteBuffer, UIntPtr.Zero, MEM_RELEASE);
            }

            resumed = true;
            Log("Injected multiplayer DLL; game PID=" + pi.dwProcessId.ToString(CultureInfo.InvariantCulture)
                + ", endpoint=" + host + ":" + port.ToString(CultureInfo.InvariantCulture) + ".");

            WaitForSingleObject(pi.hProcess, INFINITE);
            uint exitCode;
            if (GetExitCodeProcess(pi.hProcess, out exitCode))
                Log("Game exited with code " + exitCode.ToString(CultureInfo.InvariantCulture) + ".");
            return 0;
        }
        catch (Exception ex)
        {
            Log("ERROR: " + ex.ToString());
            MessageBox(IntPtr.Zero, "Не удалось напрямую запустить LEDERG CR:MP.\n\n" + ex.Message
                + "\n\nПодробности: " + LogPath, "LEDERG CR:MP", MB_ICONERROR | MB_OK);
            return 1;
        }
        finally
        {
            if (envPtr != IntPtr.Zero) Marshal.FreeHGlobal(envPtr);
            if (processCreated)
            {
                if (!resumed)
                {
                    try { TerminateProcess(pi.hProcess, 1); } catch { }
                }
                if (pi.hThread != IntPtr.Zero) CloseHandle(pi.hThread);
                if (pi.hProcess != IntPtr.Zero) CloseHandle(pi.hProcess);
            }
        }
    }
}
