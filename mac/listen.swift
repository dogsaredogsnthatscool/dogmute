// dogmute-listen: DogMute's sound source on macOS. Writes 16 kHz mono float32 samples to stdout and nothing to disk.
// The installer builds it with: xcrun swiftc -O -swift-version 5 -o bin/dogmute-listen mac/listen.swift
//
//   dogmute-listen list                  JSON {"screen": bool, "apps": [{"bundle", "name"}], "devices": [name]}
//   dogmute-listen app <bundle-prefix>   one app's own sound through ScreenCaptureKit (macOS 13+, no driver; no picture
//                                        is kept). The prefix also takes the app's helper processes.
//   dogmute-listen all                   every app's sound (still never a microphone)
//   dogmute-listen device <name>         a loopback input such as "BlackHole 2ch" (never a microphone: other names are refused)
//   --ask                                show macOS's permission prompt if it hasn't been answered yet
//
// Exit codes: 0 the source went away or changed (DogMute starts it again), 2 not found, 3 no permission, 4 needs macOS 13.
import AVFoundation
import CoreAudio
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

let RATE = 16000.0
let ask = CommandLine.arguments.contains("--ask")
let args = CommandLine.arguments.dropFirst().filter { $0 != "--ask" }

func log(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }
func fail(_ code: Int32, _ s: String) -> Never { log(s); exit(code) }

func isLoopback(_ name: String) -> Bool {
    let n = name.lowercased()
    return n.contains("blackhole") || n.contains("loopback") || n.contains("soundflower")
}

// ---------- output: anything in, 16 kHz mono float32 out ----------

let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: RATE, channels: 1, interleaved: false)!
var converter: AVAudioConverter?
var converterFrom: AVAudioFormat?
var keep = [AnyObject]()   // the engine or stream that has to stay alive

final class Box<T> { var v: T; init(_ v: T) { self.v = v } }

func emit(_ buf: AVAudioPCMBuffer) {
    var b = buf
    if buf.format != outFormat {
        if converterFrom != buf.format {
            converter = AVAudioConverter(from: buf.format, to: outFormat)
            converter?.downmix = true
            converterFrom = buf.format
        }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * RATE / buf.format.sampleRate) + 64
        guard let conv = converter, let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: cap) else { return }
        let fed = Box(false)
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if fed.v { status.pointee = .noDataNow; return nil }
            fed.v = true
            status.pointee = .haveData
            return buf
        }
        if err != nil { return }
        b = out
    }
    guard let ch = b.floatChannelData, b.frameLength > 0 else { return }
    let n = Int(b.frameLength)
    if fwrite(ch[0], 4, n, stdout) < n { exit(0) }   // DogMute went away
    fflush(stdout)
}

// ---------- loopback input devices (BlackHole) ----------

struct Device { let id: AudioDeviceID; let name: String }

func inputDevices() -> [Device] {
    let system = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids.compactMap { id in
        var streams = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioDevicePropertyScopeInput,
                                                 mElement: kAudioObjectPropertyElementMain)
        var n: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &n) == noErr, n > 0 else { return nil }   // no input side
        var nameAddr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal,
                                                  mElement: kAudioObjectPropertyElementMain)
        var name: Unmanaged<CFString>?
        var nsize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let st = withUnsafeMutablePointer(to: &name) { AudioObjectGetPropertyData(id, &nameAddr, 0, nil, &nsize, $0) }
        guard st == noErr, let s = name?.takeRetainedValue() else { return nil }
        return Device(id: id, name: s as String)
    }
}

func runDevice(_ name: String) {
    guard isLoopback(name) else { fail(2, "only a loopback device (BlackHole, Loopback), never a microphone") }
    guard let dev = inputDevices().first(where: { $0.name == name }) else { fail(2, "no sound device called \(name)") }
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: break
    case .notDetermined where ask:
        let sem = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .audio) { _ in sem.signal() }
        sem.wait()
        if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized { fail(3, "permission: microphone") }
    default: fail(3, "permission: microphone")   // macOS counts reading BlackHole as using a microphone
    }
    let engine = AVAudioEngine()
    keep.append(engine)
    let input = engine.inputNode
    var id = dev.id
    guard let unit = input.audioUnit,
          AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &id,
                               UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr
    else { fail(2, "can't open \(name)") }
    let fmt = input.outputFormat(forBus: 0)
    guard fmt.sampleRate > 0 else { fail(2, "can't read \(name)") }
    input.installTap(onBus: 0, bufferSize: 1024, format: fmt) { buf, _ in emit(buf) }
    NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in
        fail(0, "the sound devices changed")
    }
    do { try engine.start() } catch { fail(2, "can't start \(name): \(error.localizedDescription)") }
    Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
        if !inputDevices().contains(where: { $0.id == dev.id }) { fail(0, "\(name) is gone") }
    }
}

// ---------- an app's sound through ScreenCaptureKit ----------

@available(macOS 13.0, *)
final class AppSound: NSObject, SCStreamOutput, SCStreamDelegate {
    var capture: SCStream?
    let queue = DispatchQueue(label: "dogmute.listen")

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid else { return }   // the tiny picture stream is ignored
        try? sb.withAudioBufferList { list, _ in
            guard let desc = sb.formatDescription?.audioStreamBasicDescription,
                  let fmt = AVAudioFormat(standardFormatWithSampleRate: desc.mSampleRate, channels: desc.mChannelsPerFrame),
                  let pcm = AVAudioPCMBuffer(pcmFormat: fmt, bufferListNoCopy: list.unsafePointer)
            else { return }
            emit(pcm)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { fail(0, "capture stopped: \(error.localizedDescription)") }

    static func content() async throws -> SCShareableContent {
        try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    }

    func run(prefix: String?) async {
        if !CGPreflightScreenCaptureAccess() {
            if ask { _ = CGRequestScreenCaptureAccess() }
            fail(3, "permission: screen")
        }
        let content: SCShareableContent
        do { content = try await AppSound.content() } catch { fail(3, "permission: screen (\(error.localizedDescription))") }
        guard let display = content.displays.first else { fail(2, "no display") }
        let filter: SCContentFilter
        var pids = Set<pid_t>()
        if let prefix = prefix {
            let apps = content.applications.filter { $0.bundleIdentifier.hasPrefix(prefix) }
            guard !apps.isEmpty else { fail(2, "\(prefix) isn't running") }
            pids = Set(apps.map { $0.processID })
            filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
        } else {
            filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        }
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = Int(RATE)
        cfg.channelCount = 1
        cfg.excludesCurrentProcessAudio = true
        cfg.width = 2   // a picture stream has to exist; keep it tiny and slow
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.showsCursor = false
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        do {
            try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
            try await s.startCapture()
        } catch {
            let code = (error as NSError).code
            fail(code == -3801 ? 3 : 2, code == -3801 ? "permission: screen" : "can't capture: \(error.localizedDescription)")
        }
        capture = s
        guard let prefix = prefix else { return }
        while true {   // the app quit, restarted or started a helper: start over so the filter has them all
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard let c = try? await AppSound.content() else { continue }
            if Set(c.applications.filter { $0.bundleIdentifier.hasPrefix(prefix) }.map { $0.processID }) != pids {
                fail(0, "\(prefix) changed")
            }
        }
    }
}

func list() {
    var out: [String: Any] = ["screen": CGPreflightScreenCaptureAccess(),
                              "devices": inputDevices().map { $0.name }.filter(isLoopback), "apps": [[String: String]]()]
    if #available(macOS 13.0, *), CGPreflightScreenCaptureAccess() {
        let sem = DispatchSemaphore(value: 0)
        let apps = Box([[String: String]]())
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { c, _ in
            apps.v = (c?.applications ?? []).filter { !$0.applicationName.isEmpty && !$0.bundleIdentifier.isEmpty }
                .map { ["bundle": $0.bundleIdentifier, "name": $0.applicationName] }
            sem.signal()
        }
        _ = sem.wait(timeout: .now() + 5)
        out["apps"] = apps.v
    }
    let data = try! JSONSerialization.data(withJSONObject: out)
    FileHandle.standardOutput.write(data)
    exit(0)
}

switch args.first ?? "" {
case "list":
    list()
case "device" where args.count == 2:
    runDevice(args[args.startIndex + 1])
case "app" where args.count == 2, "all":
    guard #available(macOS 13.0, *) else { fail(4, "needs macOS 13 or newer") }
    let app = AppSound()
    keep.append(app)
    let prefix = args.first == "app" ? args[args.startIndex + 1] : nil
    Task { await app.run(prefix: prefix) }
default:
    fail(2, "usage: dogmute-listen list | app <bundle-id> | all | device <name> [--ask]")
}
RunLoop.main.run()   // also runs the device check's timer
