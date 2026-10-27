// dogmute-listen.exe: DogMute's sound source on Windows. Writes 16 kHz mono float32 samples to stdout and nothing to disk.
// It also lists the webcams, for the killfeed camera (dogmute/killcam.py).
// The release workflow builds it with MSVC:
//   cl /nologo /O2 /EHsc /std:c++17 win\listen.cpp /Fe:bin\dogmute-listen.exe ole32.lib shell32.lib version.lib
//
//   dogmute-listen list            JSON {"app_capture": bool, "build": n, "apps": [{"exe", "name", "pid"}], "devices": [name]}
//   dogmute-listen app <exe>       one program's own sound and its child processes' (WASAPI process loopback, Windows 10
//                                  2004 or newer, no driver). <exe> is a name prefix: "discord" also takes PTB and Canary
//   dogmute-listen pid <n>         the same for one process id and its children (the tests use it)
//   dogmute-listen all             every app's sound: a loopback of the default speakers (never a microphone)
//   dogmute-listen device <name>   a loopback of one output device, by name (only outputs: a microphone can't be picked)
//   dogmute-listen cameras         JSON {"mf": [name], "dshow": [name]}: video inputs in Media Foundation's order (the
//                                  index OpenCV's MSMF backend opens) and DirectShow's (where OBS's virtual camera is)
//
// Exit codes (dogmute/audio.py reads them): 0 DogMute went away or the speakers changed, 2 that program isn't running or
// that device is gone, 4 this Windows is too old to hear one program on its own, 1 anything else (stderr says what).
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <functiondiscoverykeys_devpkey.h>
#include <mfapi.h>
#include <mfidl.h>
#include <dshow.h>
#include <shellapi.h>
#include <tlhelp32.h>
#include <io.h>
#include <fcntl.h>
#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>
#include <cwctype>

#if __has_include(<audioclientactivationparams.h>)
#include <audioclientactivationparams.h>
#else   // older SDKs and MinGW: the same layout as Windows' own header
typedef enum { AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0, AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1 } AUDIOCLIENT_ACTIVATION_TYPE;
typedef enum { PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0,
               PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1 } PROCESS_LOOPBACK_MODE;
typedef struct { DWORD TargetProcessId; PROCESS_LOOPBACK_MODE ProcessLoopbackMode; } AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS;
typedef struct {
    AUDIOCLIENT_ACTIVATION_TYPE ActivationType;
    union { AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS ProcessLoopbackParams; };
} AUDIOCLIENT_ACTIVATION_PARAMS;
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

#ifdef _MSC_VER
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "version.lib")
#pragma comment(lib, "mf.lib")
#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "strmiids.lib")
#endif

static const int RATE = 16000;
static const DWORD APP_CAPTURE_BUILD = 19041;   // Windows 10 2004: the first with process loopback

static void log(const char* fmt, ...) {
    va_list a;
    va_start(a, fmt);
    vfprintf(stderr, fmt, a);
    va_end(a);
    fputc('\n', stderr);
    fflush(stderr);
}

[[noreturn]] static void fail(int code, const char* what, HRESULT hr = S_OK) {
    if (hr != S_OK) log("%s (0x%08lx)", what, (unsigned long)hr); else log("%s", what);
    ExitProcess(code);
}

static std::wstring lower(std::wstring s) {
    for (auto& c : s) c = (wchar_t)towlower(c);
    return s;
}

static std::string utf8(const std::wstring& w) {
    if (w.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}

static std::string json(const std::wstring& w) {
    std::string out = "\"";
    for (unsigned char c : utf8(w)) {
        if (c == '"' || c == '\\') { out += '\\'; out += (char)c; }
        else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); out += b; }
        else out += (char)c;
    }
    return out + "\"";
}

static DWORD build_number() {
    typedef LONG (WINAPI *RtlGetVersionFn)(OSVERSIONINFOW*);
    auto fn = (RtlGetVersionFn)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "RtlGetVersion");
    OSVERSIONINFOW v{sizeof v};
    return fn && fn(&v) == 0 ? v.dwBuildNumber : 0;
}

// ---- processes ----

struct Proc { DWORD pid, ppid; std::wstring exe; };

static std::vector<Proc> processes() {
    std::vector<Proc> out;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return out;
    PROCESSENTRY32W e{sizeof e};
    for (BOOL ok = Process32FirstW(snap, &e); ok; ok = Process32NextW(snap, &e))
        out.push_back({e.th32ProcessID, e.th32ParentProcessID, e.szExeFile});
    CloseHandle(snap);
    return out;
}

static bool matches(const std::wstring& exe, const std::wstring& want) {   // want is lower case
    std::wstring e = lower(exe);
    if (want.size() > 4 && want.compare(want.size() - 4, 4, L".exe") == 0) return e == want;
    return e.compare(0, want.size(), want) == 0;
}

// The program's first process (its parent isn't the same program), the one with the most children of its own.
static DWORD find_root(const std::wstring& want) {
    auto ps = processes();
    DWORD best = 0;
    int best_kids = -1;
    for (auto& p : ps) {
        if (!matches(p.exe, want)) continue;
        bool child = false;
        for (auto& q : ps) if (q.pid == p.ppid && matches(q.exe, want)) child = true;
        if (child) continue;
        int kids = 0;
        for (auto& q : ps) if (q.ppid == p.pid) kids++;
        if (kids > best_kids) best = p.pid, best_kids = kids;
    }
    return best;
}

static std::wstring describe(DWORD pid, const std::wstring& exe) {   // "Discord" from the exe's version info
    std::wstring name = exe.size() > 4 ? exe.substr(0, exe.size() - 4) : exe;
    HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!h) return name;
    wchar_t path[MAX_PATH * 2];
    DWORD n = MAX_PATH * 2;
    if (QueryFullProcessImageNameW(h, 0, path, &n)) {
        DWORD z = 0, size = GetFileVersionInfoSizeW(path, &z);
        std::vector<BYTE> info(size);
        struct Lang { WORD lang, cp; } *langs = nullptr;
        UINT len = 0;
        if (size && GetFileVersionInfoW(path, 0, size, info.data())
            && VerQueryValueW(info.data(), L"\\VarFileInfo\\Translation", (void**)&langs, &len) && len >= sizeof(Lang)) {
            wchar_t key[64];
            swprintf(key, 64, L"\\StringFileInfo\\%04x%04x\\FileDescription", langs[0].lang, langs[0].cp);
            wchar_t* desc = nullptr;
            if (VerQueryValueW(info.data(), key, (void**)&desc, &len) && len > 1 && desc[0]) name = desc;
        }
    }
    CloseHandle(h);
    return name;
}

// ---- devices ----

static IMMDeviceEnumerator* enumerator() {
    IMMDeviceEnumerator* en = nullptr;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&en));
    if (FAILED(hr)) fail(1, "no Windows audio", hr);
    return en;
}

static std::wstring friendly(IMMDevice* d) {
    IPropertyStore* ps = nullptr;
    std::wstring name;
    if (SUCCEEDED(d->OpenPropertyStore(STGM_READ, &ps))) {
        PROPVARIANT v;
        PropVariantInit(&v);
        if (SUCCEEDED(ps->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR) name = v.pwszVal;
        PropVariantClear(&v);
        ps->Release();
    }
    return name;
}

static std::vector<IMMDevice*> outputs(IMMDeviceEnumerator* en) {   // speakers and headphones only (eRender)
    std::vector<IMMDevice*> out;
    IMMDeviceCollection* all = nullptr;
    UINT n = 0;
    if (FAILED(en->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &all))) return out;
    all->GetCount(&n);
    for (UINT i = 0; i < n; i++) {
        IMMDevice* d = nullptr;
        if (SUCCEEDED(all->Item(i, &d))) out.push_back(d);
    }
    all->Release();
    return out;
}

static int list() {
    auto ps = processes();
    auto exe_of = [&](DWORD pid) -> const Proc* { for (auto& p : ps) if (p.pid == pid) return &p; return nullptr; };
    std::vector<DWORD> pids;
    std::vector<std::wstring> seen, devices;
    IMMDeviceEnumerator* en = enumerator();
    for (IMMDevice* d : outputs(en)) {
        devices.push_back(friendly(d));
        IAudioSessionManager2* mgr = nullptr;
        IAudioSessionEnumerator* se = nullptr;
        if (SUCCEEDED(d->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr))
            && SUCCEEDED(mgr->GetSessionEnumerator(&se))) {
            int n = 0;
            se->GetCount(&n);
            for (int i = 0; i < n; i++) {
                IAudioSessionControl* c = nullptr;
                IAudioSessionControl2* c2 = nullptr;
                DWORD pid = 0;
                if (SUCCEEDED(se->GetSession(i, &c)) && SUCCEEDED(c->QueryInterface(IID_PPV_ARGS(&c2)))) {
                    if (c2->IsSystemSoundsSession() != S_OK && SUCCEEDED(c2->GetProcessId(&pid)) && pid) pids.push_back(pid);
                    c2->Release();
                }
                if (c) c->Release();
            }
            se->Release();
        }
        if (mgr) mgr->Release();
        d->Release();
    }
    en->Release();
    if (DWORD discord = find_root(L"discord")) pids.insert(pids.begin(), discord);   // even before it plays anything

    std::string out = "{\"app_capture\": " + std::string(build_number() >= APP_CAPTURE_BUILD ? "true" : "false")
                      + ", \"build\": " + std::to_string(build_number()) + ", \"apps\": [";
    bool first = true;
    for (DWORD pid : pids) {
        const Proc* p = exe_of(pid);
        if (!p || pid == GetCurrentProcessId()) continue;
        // a helper process playing for its program (Discord's voice): list the program, under its own exe name
        for (const Proc* up = exe_of(p->ppid); up && matches(up->exe, lower(p->exe)); up = exe_of(up->ppid)) p = up;
        std::wstring key = lower(p->exe);
        bool dup = false;
        for (auto& s : seen) dup |= s == key;
        if (dup) continue;
        seen.push_back(key);
        out += std::string(first ? "" : ", ") + "{\"exe\": " + json(p->exe) + ", \"name\": " + json(describe(p->pid, p->exe))
               + ", \"pid\": " + std::to_string(p->pid) + "}";
        first = false;
    }
    out += "], \"devices\": [";
    for (size_t i = 0; i < devices.size(); i++) out += (i ? ", " : "") + json(devices[i]);
    out += "]}\n";
    fwrite(out.data(), 1, out.size(), stdout);
    return 0;
}

// ---- webcams ----

static std::vector<std::wstring> mf_cameras() {
    std::vector<std::wstring> out;
    if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_LITE))) return out;
    IMFAttributes* a = nullptr;
    IMFActivate** devs = nullptr;
    UINT32 n = 0;
    if (SUCCEEDED(MFCreateAttributes(&a, 1))
        && SUCCEEDED(a->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID))
        && SUCCEEDED(MFEnumDeviceSources(a, &devs, &n))) {
        for (UINT32 i = 0; i < n; i++) {
            WCHAR* name = nullptr;
            UINT32 len = 0;
            if (SUCCEEDED(devs[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &name, &len))) {
                out.push_back(name);
                CoTaskMemFree(name);
            } else {
                out.push_back(L"?");
            }
            devs[i]->Release();
        }
        CoTaskMemFree(devs);
    }
    if (a) a->Release();
    MFShutdown();
    return out;
}

static std::vector<std::wstring> dshow_cameras() {
    std::vector<std::wstring> out;
    ICreateDevEnum* de = nullptr;
    IEnumMoniker* em = nullptr;
    if (FAILED(CoCreateInstance(CLSID_SystemDeviceEnum, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&de)))) return out;
    if (de->CreateClassEnumerator(CLSID_VideoInputDeviceCategory, &em, 0) == S_OK) {
        IMoniker* m = nullptr;
        while (em->Next(1, &m, nullptr) == S_OK) {
            IPropertyBag* bag = nullptr;
            VARIANT v;
            VariantInit(&v);
            if (SUCCEEDED(m->BindToStorage(nullptr, nullptr, IID_PPV_ARGS(&bag)))) {
                out.push_back(SUCCEEDED(bag->Read(L"FriendlyName", &v, nullptr)) && v.vt == VT_BSTR ? v.bstrVal : L"?");
                VariantClear(&v);
                bag->Release();
            }
            m->Release();
        }
        em->Release();
    }
    de->Release();
    return out;
}

static int cameras() {
    auto arr = [](const std::vector<std::wstring>& v) {
        std::string s = "[";
        for (size_t i = 0; i < v.size(); i++) s += (i ? ", " : "") + json(v[i]);
        return s + "]";
    };
    std::string out = "{\"mf\": " + arr(mf_cameras()) + ", \"dshow\": " + arr(dshow_cameras()) + "}\n";
    fwrite(out.data(), 1, out.size(), stdout);
    return 0;
}

// ---- turning whatever Windows hands us into 16 kHz mono float ----

struct Format { int rate = 0, channels = 0, bits = 0; bool is_float = false; };

static Format parse(const WAVEFORMATEX* w) {
    Format f{(int)w->nSamplesPerSec, (int)w->nChannels, (int)w->wBitsPerSample, w->wFormatTag == WAVE_FORMAT_IEEE_FLOAT};
    if (w->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        auto x = (const WAVEFORMATEXTENSIBLE*)w;
        f.is_float = IsEqualGUID(x->SubFormat, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT);
    }
    return f;
}

struct Resampler {   // a low-pass at 7 kHz, then linear steps at 16 kHz (whisper hears nothing above 8 kHz anyway)
    int rate;
    double step, pos = 0;
    std::vector<float> taps, hist, buf;
    size_t h = 0;

    explicit Resampler(int r) : rate(r), step(r / (double)RATE) {
        if (rate == RATE) return;
        const int N = 63;
        const double fc = 7000.0 / rate, pi = 3.14159265358979323846;
        double sum = 0;
        for (int i = 0; i < N; i++) {
            double m = i - (N - 1) / 2.0;
            double s = m == 0 ? 2 * fc : sin(2 * pi * fc * m) / (pi * m);
            double w = 0.5 - 0.5 * cos(2 * pi * i / (N - 1));
            taps.push_back((float)(s * w));
            sum += s * w;
        }
        for (auto& t : taps) t = (float)(t / sum);
        hist.assign(N, 0.f);
    }

    void push(const float* x, size_t n, std::vector<float>& out) {
        if (rate == RATE) { out.insert(out.end(), x, x + n); return; }
        const size_t N = taps.size();
        for (size_t k = 0; k < n; k++) {
            hist[h] = x[k];
            h = (h + 1) % N;
            float acc = 0;
            for (size_t i = 0; i < N; i++) acc += taps[i] * hist[(h + i) % N];
            buf.push_back(acc);
        }
        while (pos + 1 < buf.size()) {
            size_t i = (size_t)pos;
            double f = pos - i;
            out.push_back((float)(buf[i] * (1 - f) + buf[i + 1] * f));
            pos += step;
        }
        size_t drop = std::min((size_t)pos, buf.size());
        buf.erase(buf.begin(), buf.begin() + drop);
        pos -= drop;
    }
};

static void mono(const BYTE* data, UINT32 frames, const Format& f, std::vector<float>& out) {
    out.resize(frames);
    for (UINT32 i = 0; i < frames; i++) {
        double s = 0;
        for (int c = 0; c < f.channels; c++) {
            size_t k = (size_t)i * f.channels + c;
            if (f.is_float) s += ((const float*)data)[k];
            else if (f.bits == 16) s += ((const int16_t*)data)[k] / 32768.0;
            else if (f.bits == 32) s += ((const int32_t*)data)[k] / 2147483648.0;
            else if (f.bits == 24) {
                const BYTE* b = data + k * 3;
                s += (int32_t)((b[0] << 8) | (b[1] << 16) | (b[2] << 24)) / 2147483648.0;
            }
        }
        out[i] = (float)(s / f.channels);
    }
}

static void put(const float* x, size_t n) {
    if (n && fwrite(x, sizeof(float), n, stdout) < n) ExitProcess(0);   // DogMute went away
    fflush(stdout);
}

// ---- capture ----

static const DWORD FLAGS = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                           | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;

static WAVEFORMATEX pcm(int rate, int channels, bool is_float) {
    WAVEFORMATEX w{};
    w.wFormatTag = is_float ? WAVE_FORMAT_IEEE_FLOAT : WAVE_FORMAT_PCM;
    w.nChannels = (WORD)channels;
    w.nSamplesPerSec = rate;
    w.wBitsPerSample = is_float ? 32 : 16;
    w.nBlockAlign = (WORD)(channels * w.wBitsPerSample / 8);
    w.nAvgBytesPerSec = rate * w.nBlockAlign;
    return w;
}

// Asks for 16 kHz mono first (Windows converts), then what the speakers run at.
static Format start(IAudioClient* ac, bool process) {
    WAVEFORMATEX tries[] = {pcm(RATE, 1, true), pcm(48000, 2, true), pcm(44100, 2, false)};
    HRESULT hr = E_FAIL;
    for (auto& w : tries)
        if (SUCCEEDED(hr = ac->Initialize(AUDCLNT_SHAREMODE_SHARED, FLAGS, 2000000, 0, &w, nullptr))) return parse(&w);
    if (!process) {   // the speakers' own format, as they run it
        WAVEFORMATEX* mix = nullptr;
        if (SUCCEEDED(ac->GetMixFormat(&mix))) {
            hr = ac->Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                                2000000, 0, mix, nullptr);
            Format f = parse(mix);
            CoTaskMemFree(mix);
            if (SUCCEEDED(hr)) return f;
        }
    }
    fail(1, "Windows refused every sound format", hr);
}

static int capture(IAudioClient* ac, bool process, HANDLE watch) {
    Format f = start(ac, process);
    HANDLE ev = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    IAudioCaptureClient* cc = nullptr;
    HRESULT hr = ac->SetEventHandle(ev);
    if (SUCCEEDED(hr)) hr = ac->GetService(IID_PPV_ARGS(&cc));
    if (SUCCEEDED(hr)) hr = ac->Start();
    if (FAILED(hr)) fail(1, "couldn't start listening", hr);
    log("listening: %d Hz, %d ch, %d bit%s", f.rate, f.channels, f.bits, f.is_float ? " float" : "");

    Resampler rs(f.rate);
    std::vector<float> in, out;
    LARGE_INTEGER qf, t0, now;
    QueryPerformanceFrequency(&qf);
    QueryPerformanceCounter(&t0);
    double emitted = 0, last_packet = 0;
    for (;;) {
        WaitForSingleObject(ev, 10);   // also a 10 ms poll, for Windows versions that don't signal loopback events
        if (watch && WaitForSingleObject(watch, 0) == WAIT_OBJECT_0) fail(2, "the program closed");
        UINT32 n = 0;
        while (SUCCEEDED(hr = cc->GetNextPacketSize(&n)) && n) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            if (FAILED(hr = cc->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) in.assign(frames, 0.f);
            else mono(data, frames, f, in);
            cc->ReleaseBuffer(frames);
            out.clear();
            rs.push(in.data(), in.size(), out);
            put(out.data(), out.size());
            emitted += out.size();
            QueryPerformanceCounter(&now);
            last_packet = (now.QuadPart - t0.QuadPart) / (double)qf.QuadPart;
        }
        if (hr == AUDCLNT_E_DEVICE_INVALIDATED) fail(0, "the speakers changed");
        if (FAILED(hr)) fail(1, "listening stopped", hr);
        // Windows sends nothing while nothing plays: send that silence too, so every sample keeps its real time
        QueryPerformanceCounter(&now);
        double t = (now.QuadPart - t0.QuadPart) / (double)qf.QuadPart;
        if (t - last_packet > 0.1 && t * RATE - emitted > 0.04 * RATE) {
            size_t gap = (size_t)(t * RATE - emitted - 0.02 * RATE);
            out.assign(gap, 0.f);
            put(out.data(), gap);
            emitted += gap;
        }
    }
}

struct Done : IActivateAudioInterfaceCompletionHandler, IAgileObject {   // Windows calls this back on its own thread
    HANDLE ev = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    LONG refs = 1;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id, void** out) override {
        if (IsEqualIID(id, __uuidof(IUnknown)) || IsEqualIID(id, __uuidof(IActivateAudioInterfaceCompletionHandler)))
            *out = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
        else if (IsEqualIID(id, __uuidof(IAgileObject))) *out = static_cast<IAgileObject*>(this);
        else { *out = nullptr; return E_NOINTERFACE; }
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs); }
    ULONG STDMETHODCALLTYPE Release() override { return InterlockedDecrement(&refs); }   // lives until the process ends
    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation*) override { SetEvent(ev); return S_OK; }
};

static int hear(DWORD pid, const std::wstring& what) {
    HANDLE watch = OpenProcess(SYNCHRONIZE, FALSE, pid);
    if (!watch) fail(2, "that program isn't running");
    bool old = build_number() < APP_CAPTURE_BUILD;

    typedef HRESULT (WINAPI *ActivateFn)(LPCWSTR, REFIID, PROPVARIANT*, IActivateAudioInterfaceCompletionHandler*,
                                         IActivateAudioInterfaceAsyncOperation**);
    auto activate = (ActivateFn)GetProcAddress(LoadLibraryW(L"mmdevapi.dll"), "ActivateAudioInterfaceAsync");
    if (!activate) fail(4, "this Windows can't hear one program on its own");

    AUDIOCLIENT_ACTIVATION_PARAMS params{};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT pv;
    PropVariantInit(&pv);
    pv.vt = VT_BLOB;
    pv.blob.cbSize = sizeof params;
    pv.blob.pBlobData = (BYTE*)&params;

    Done* done = new Done;
    IActivateAudioInterfaceAsyncOperation* op = nullptr;
    HRESULT hr = activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &pv, done, &op);
    if (FAILED(hr)) fail(old ? 4 : 1, "Windows won't hear that program on its own", hr);
    if (WaitForSingleObject(done->ev, 10000) != WAIT_OBJECT_0) fail(1, "Windows didn't answer");
    HRESULT result = E_FAIL;
    IUnknown* unk = nullptr;
    IAudioClient* ac = nullptr;
    hr = op->GetActivateResult(&result, &unk);
    if (SUCCEEDED(hr)) hr = result;
    if (SUCCEEDED(hr)) hr = unk->QueryInterface(IID_PPV_ARGS(&ac));
    if (FAILED(hr)) fail(old ? 4 : 1, "Windows won't hear that program on its own", hr);
    log("hearing %ls (process %lu and its children)", what.c_str(), (unsigned long)pid);
    return capture(ac, true, watch);
}

static int app(const std::wstring& want) {
    DWORD pid = find_root(lower(want));
    if (!pid) fail(2, "that program isn't running");
    return hear(pid, want);
}

static int device(const std::wstring* name) {   // nullptr = the default speakers
    IMMDeviceEnumerator* en = enumerator();
    IMMDevice* dev = nullptr;
    if (!name) {
        if (FAILED(en->GetDefaultAudioEndpoint(eRender, eConsole, &dev))) fail(2, "no speakers or headphones");
    } else {
        for (IMMDevice* d : outputs(en)) {
            if (!dev && lower(friendly(d)) == lower(*name)) dev = d;
            else d->Release();
        }
        if (!dev) fail(2, "that output device is gone");
    }
    IAudioClient* ac = nullptr;
    HRESULT hr = dev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&ac);
    if (FAILED(hr)) fail(2, "that output device can't be heard", hr);
    log("hearing everything on %ls", friendly(dev).c_str());
    return capture(ac, false, nullptr);
}

int main() {
    int argc = 0;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    _setmode(_fileno(stdout), _O_BINARY);
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) fail(1, "COM didn't start", hr);
    std::wstring cmd = argc > 1 ? argv[1] : L"";
    if (cmd == L"list") return list();
    if (cmd == L"cameras") return cameras();
    if (cmd == L"app" && argc > 2) return app(argv[2]);
    if (cmd == L"pid" && argc > 2) return hear(wcstoul(argv[2], nullptr, 10), L"that process");
    if (cmd == L"all") return device(nullptr);
    if (cmd == L"device" && argc > 2) { std::wstring n = argv[2]; return device(&n); }
    fail(2, "usage: dogmute-listen list | cameras | app <exe> | pid <n> | all | device <name>");
}
