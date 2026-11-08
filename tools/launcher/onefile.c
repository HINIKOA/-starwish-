/* 星愿记忆-windows-amd64.exe —— 单文件版启动器。
 *
 * 文件结构：[本程序] [zip 负载] [尾部 64 字节：魔数 + 偏移 + 大小 + 版本标签]
 *
 * 启动流程：
 *   1. 读取自身尾部，得到负载位置与版本标签（版本号 + 内容哈希）。
 *   2. 若 %LOCALAPPDATA%\StarWish\runtime\<标签>\.ok 已存在，直接启动（第二次起不再解压）。
 *      否则显示「首次启动，正在准备…」小窗，解压到 <标签>.tmp，完成后改名并写 .ok。
 *      解压中途被打断不会留下半成品：下次会重新解压。
 *   3. 删除旧版本的缓存目录（尽力而为）。
 *   4. 记忆数据放在 exe 旁边的 soul\ 文件夹（STARWISH_SOUL_DIR），随 U 盘带走。
 *   5. 无命令窗口运行 runtime\pythonw.exe desktop.py；关窗口即退出。
 *      参数 --mcp：以 stdio 方式运行 MCP 服务（继承标准输入输出），供 Claude / Cursor 等调用。
 *      参数 --notify <客户端>：AI 客户端「本轮结束」钩子；继承标准输入输出、不显示进度窗，立刻退出。
 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shlobj.h>
#include <commctrl.h>
#include <wchar.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "miniz/miniz.h"

#define MAGIC "SWPKG01\0"
#define TITLE L"星愿记忆"

#pragma pack(push, 1)
typedef struct {
    char magic[8];
    uint64_t offset;
    uint64_t size;
    char tag[40];
} Trailer;
#pragma pack(pop)

static HWND g_win = NULL, g_text = NULL, g_bar = NULL;

static void fail(const wchar_t *msg) { MessageBoxW(NULL, msg, TITLE, MB_ICONERROR); }

static void pump(void) {
    MSG m;
    while (PeekMessageW(&m, NULL, 0, 0, PM_REMOVE)) { TranslateMessage(&m); DispatchMessageW(&m); }
}

static LRESULT CALLBACK wndproc(HWND h, UINT msg, WPARAM w, LPARAM l) {
    if (msg == WM_CLOSE) return 0; /* 解压中不允许关 */
    return DefWindowProcW(h, msg, w, l);
}

static void show_progress(HINSTANCE inst) {
    WNDCLASSW wc; ZeroMemory(&wc, sizeof wc);
    wc.lpfnWndProc = wndproc; wc.hInstance = inst; wc.lpszClassName = L"StarWishPrep";
    wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1)); wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_WAIT);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    INITCOMMONCONTROLSEX ic = {sizeof ic, ICC_PROGRESS_CLASS};
    InitCommonControlsEx(&ic);
    RegisterClassW(&wc);
    int W = 420, H = 140;
    int x = (GetSystemMetrics(SM_CXSCREEN) - W) / 2, y = (GetSystemMetrics(SM_CYSCREEN) - H) / 2;
    g_win = CreateWindowExW(WS_EX_TOPMOST, wc.lpszClassName, TITLE, WS_CAPTION | WS_VISIBLE,
                            x, y, W, H, NULL, NULL, inst, NULL);
    HFONT f = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
    g_text = CreateWindowW(L"STATIC", L"首次启动，正在准备运行环境（只需一次）…",
                           WS_CHILD | WS_VISIBLE, 20, 18, 370, 22, g_win, NULL, inst, NULL);
    SendMessageW(g_text, WM_SETFONT, (WPARAM)f, TRUE);
    g_bar = CreateWindowW(L"msctls_progress32", NULL, WS_CHILD | WS_VISIBLE, 20, 50, 370, 20,
                          g_win, NULL, inst, NULL);
    SendMessageW(g_bar, PBM_SETRANGE32, 0, 1000);
    pump();
}

static int mkdirs(wchar_t *path) { /* path 会被临时修改；支持 \\?\ 长路径前缀 */
    wchar_t *p = path + (wcsncmp(path, L"\\\\?\\", 4) == 0 ? 7 : 3);
    for (; *p; p++) {
        if (*p == L'\\') { *p = 0; CreateDirectoryW(path, NULL); *p = L'\\'; }
    }
    return CreateDirectoryW(path, NULL) || GetLastError() == ERROR_ALREADY_EXISTS;
}

static size_t write_cb(void *op, mz_uint64 ofs, const void *buf, size_t n) {
    (void)ofs;
    DWORD done = 0;
    return WriteFile((HANDLE)op, buf, (DWORD)n, &done, NULL) ? done : 0;
}

static void rmtree(const wchar_t *dir) {
    wchar_t buf[MAX_PATH * 4 + 2];
    size_t n = wcslen(dir);
    if (n + 2 >= MAX_PATH * 4) return;
    wcscpy(buf, dir); buf[n + 1] = 0; /* SHFileOperation 需要双零结尾 */
    SHFILEOPSTRUCTW op; ZeroMemory(&op, sizeof op);
    op.wFunc = FO_DELETE; op.pFrom = buf;
    op.fFlags = FOF_NO_UI;
    SHFileOperationW(&op);
}

static int extract(const unsigned char *data, size_t size, const wchar_t *dest) {
    mz_zip_archive z; ZeroMemory(&z, sizeof z);
    if (!mz_zip_reader_init_mem(&z, data, size, 0)) return 0;
    mz_uint count = mz_zip_reader_get_num_files(&z);
    wchar_t wname[MAX_PATH * 2], full[MAX_PATH * 4];
    int ok = 1;
    for (mz_uint i = 0; i < count && ok; i++) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&z, i, &st)) { ok = 0; break; }
        if (!MultiByteToWideChar(CP_UTF8, 0, st.m_filename, -1, wname, MAX_PATH * 2)) { ok = 0; break; }
        for (wchar_t *p = wname; *p; p++) if (*p == L'/') *p = L'\\';
        if (wcsstr(wname, L"..")) continue; /* 防目录穿越 */
        _snwprintf(full, MAX_PATH * 4, L"\\\\?\\%ls\\%ls", dest, wname);
        full[MAX_PATH * 4 - 1] = 0;
        size_t L = wcslen(full);
        if (mz_zip_reader_is_file_a_directory(&z, i)) {
            if (L && full[L - 1] == L'\\') full[L - 1] = 0;
            mkdirs(full);
            continue;
        }
        wchar_t *slash = wcsrchr(full, L'\\');
        *slash = 0; mkdirs(full); *slash = L'\\';
        HANDLE fh = CreateFileW(full, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (fh == INVALID_HANDLE_VALUE) { ok = 0; break; }
        if (!mz_zip_reader_extract_to_callback(&z, i, write_cb, fh, 0)) ok = 0;
        CloseHandle(fh);
        if (g_bar && (i % 20 == 0 || i + 1 == count)) {
            SendMessageW(g_bar, PBM_SETPOS, (WPARAM)((i + 1) * 1000ULL / count), 0);
            pump();
        }
    }
    mz_zip_reader_end(&z);
    return ok;
}

/* 删除 base 下除 keep 以外的旧版本目录 */
static void clean_old(const wchar_t *base, const wchar_t *keep) {
    wchar_t pat[MAX_PATH * 2], p[MAX_PATH * 2];
    _snwprintf(pat, MAX_PATH * 2, L"%ls\\*", base);
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW(pat, &fd);
    if (h == INVALID_HANDLE_VALUE) return;
    do {
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
        if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L"..") || !wcscmp(fd.cFileName, keep)) continue;
        _snwprintf(p, MAX_PATH * 2, L"%ls\\%ls", base, fd.cFileName);
        rmtree(p); /* 若旧版本正在运行会删除失败，下次再删 */
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, LPWSTR args, int show) {
    (void)prev; (void)show;
    wchar_t exe[MAX_PATH * 2], dir[MAX_PATH * 2];
    if (!GetModuleFileNameW(NULL, exe, MAX_PATH * 2)) return 1;
    wcscpy(dir, exe);
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;

    int mcp = args && wcsstr(args, L"--mcp") != NULL;
    int notify = args && wcsstr(args, L"--notify") != NULL;
    int console = mcp || notify;   /* 需要标准输入输出的模式 */

    /* 1. 读尾部 + 映射负载 */
    HANDLE f = CreateFileW(exe, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (f == INVALID_HANDLE_VALUE) { fail(L"无法读取程序文件。"); return 2; }
    LARGE_INTEGER fsz; GetFileSizeEx(f, &fsz);
    Trailer t; DWORD got = 0;
    LARGE_INTEGER pos; pos.QuadPart = fsz.QuadPart - (LONGLONG)sizeof t;
    SetFilePointerEx(f, pos, NULL, FILE_BEGIN);
    if (!ReadFile(f, &t, sizeof t, &got, NULL) || got != sizeof t || memcmp(t.magic, MAGIC, 8)) {
        CloseHandle(f); fail(L"程序文件不完整（可能下载未完成），请重新下载。"); return 3;
    }
    t.tag[sizeof t.tag - 1] = 0;
    wchar_t tag[48];
    MultiByteToWideChar(CP_UTF8, 0, t.tag, -1, tag, 48);

    /* 2. 缓存目录 */
    wchar_t local[MAX_PATH], base[MAX_PATH * 2], target[MAX_PATH * 2], tmp[MAX_PATH * 2], okf[MAX_PATH * 2];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, local))) {
        GetTempPathW(MAX_PATH, local);
    }
    _snwprintf(base, MAX_PATH * 2, L"%ls\\StarWish\\runtime", local);
    _snwprintf(target, MAX_PATH * 2, L"%ls\\%ls", base, tag);
    _snwprintf(tmp, MAX_PATH * 2, L"%ls.tmp", target);
    _snwprintf(okf, MAX_PATH * 2, L"%ls\\.ok", target);

    if (GetFileAttributesW(okf) == INVALID_FILE_ATTRIBUTES) {
        /* 首次（或升级后）解压 */
        HANDLE map = CreateFileMappingW(f, NULL, PAGE_READONLY, 0, 0, NULL);
        const unsigned char *view = map ? (const unsigned char *)MapViewOfFile(map, FILE_MAP_READ, 0, 0, 0) : NULL;
        if (!view) { CloseHandle(f); fail(L"内存不足，无法准备运行环境。"); return 4; }
        if (!console) show_progress(inst);
        wchar_t b2[MAX_PATH * 2]; wcscpy(b2, base); mkdirs(b2);
        rmtree(tmp); rmtree(target);
        wchar_t t2[MAX_PATH * 2]; wcscpy(t2, tmp); mkdirs(t2);
        int ok = extract(view + t.offset, (size_t)t.size, tmp);
        UnmapViewOfFile(view); CloseHandle(map);
        if (ok) ok = MoveFileW(tmp, target);
        if (ok) {
            HANDLE o = CreateFileW(okf, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_HIDDEN, NULL);
            ok = o != INVALID_HANDLE_VALUE; if (ok) CloseHandle(o);
        }
        if (g_win) { DestroyWindow(g_win); g_win = NULL; pump(); }
        if (!ok) {
            CloseHandle(f); rmtree(tmp);
            fail(L"准备运行环境失败。\n请确认磁盘空间充足（约需 300MB），并检查杀毒软件是否拦截。");
            return 5;
        }
        clean_old(base, tag);
    }
    CloseHandle(f);

    /* 3. 启动 */
    wchar_t py[MAX_PATH * 2], script[MAX_PATH * 2], soul[MAX_PATH * 2], cmd[MAX_PATH * 8];
    _snwprintf(py, MAX_PATH * 2, console ? L"%ls\\runtime\\python.exe" : L"%ls\\runtime\\pythonw.exe", target);
    _snwprintf(script, MAX_PATH * 2, mcp ? L"%ls\\mcp_server.py" : L"%ls\\desktop.py", target);
    _snwprintf(soul, MAX_PATH * 2, L"%ls\\soul", dir);
    if (GetEnvironmentVariableW(L"STARWISH_SOUL_DIR", NULL, 0) == 0)
        SetEnvironmentVariableW(L"STARWISH_SOUL_DIR", soul);
    SetEnvironmentVariableW(L"STARWISH_EXE", exe);
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    if (mcp) {
        _snwprintf(cmd, MAX_PATH * 8, L"\"%ls\" \"%ls\"", py, script);
    } else {
        _snwprintf(cmd, MAX_PATH * 8, L"\"%ls\" \"%ls\" %ls", py, script, args ? args : L"");
    }

    STARTUPINFOW si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    BOOL inherit = FALSE;
    if (console) { /* 把客户端给的管道原样交给 Python（MCP 的标准输入输出 / 钩子 JSON） */
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
        inherit = TRUE;
    }
    if (!CreateProcessW(py, cmd, NULL, NULL, inherit, CREATE_NO_WINDOW, NULL, target, &si, &pi)) {
        /* 缓存可能被清理软件删坏：删掉标记，下次重新解压 */
        DeleteFileW(okf);
        fail(L"启动失败，请再次打开；若仍失败，请检查杀毒软件是否拦截。");
        return 6;
    }
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0; GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    return (int)code;
}
