/* 星愿记忆.exe —— 无命令窗口启动器。
 * 找到同目录下的 runtime\pythonw.exe，运行 desktop.py；没有控制台窗口。
 * 桌面窗口关闭时 desktop.py 会自己停掉本地服务并退出。
 * 参数 --notify <客户端>：AI 客户端钩子调用，改用 python.exe 并继承标准输入输出（读钩子 JSON），立刻退出。 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, LPWSTR args, int show) {
    wchar_t dir[MAX_PATH * 2], py[MAX_PATH * 2], script[MAX_PATH * 2], cmd[MAX_PATH * 6];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH * 2);
    if (!n) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = 0;
    int notify = args && wcsstr(args, L"--notify") != NULL;
    swprintf(py, MAX_PATH * 2, notify ? L"%ls\\runtime\\python.exe" : L"%ls\\runtime\\pythonw.exe", dir);
    swprintf(script, MAX_PATH * 2, L"%ls\\desktop.py", dir);
    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"找不到内置运行环境 runtime\\pythonw.exe。\n请把完整的「星愿记忆」文件夹一起复制，不要只复制 exe。",
                    L"星愿记忆", MB_ICONERROR);
        return 2;
    }
    swprintf(cmd, MAX_PATH * 6, L"\"%ls\" \"%ls\" %ls", py, script, args ? args : L"");
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    STARTUPINFOW si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    BOOL inherit = FALSE;
    if (notify) {
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
        inherit = TRUE;
    }
    if (!CreateProcessW(py, cmd, NULL, NULL, inherit, CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"启动失败，请确认文件夹完整、没有被杀毒软件拦截。", L"星愿记忆", MB_ICONERROR);
        return 3;
    }
    CloseHandle(pi.hThread);
    /* 等待子进程结束，这样任务管理器里关掉 exe 也能看出软件是否还在运行 */
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0; GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    return (int)code;
}
