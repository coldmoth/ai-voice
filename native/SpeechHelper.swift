import Foundation
import AVFoundation
import Speech
import CoreAudio
import AudioToolbox
import Darwin

private let outputLock = NSLock()
private func emit(_ event: String, id: String = "", _ fields: [String: Any] = [:]) {
    outputLock.lock()
    defer { outputLock.unlock() }
    var value = fields
    value["event"] = event
    value["utterance_id"] = id
    value["time_ms"] = ProcessInfo.processInfo.systemUptime * 1000
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}
private func fatal(_ code: String, _ message: String) -> Never {
    emit("error", ["code": code, "message": message, "fatal": true])
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

private struct Options {
    var input = "MIC", language = "ru-RU"
    var endpointMS = 450.0, thresholdDB = -45.0
    var doctor = false, authorize = false
    var locales = false
    var only: String?
    var rawAudio = false
    var eventSocket: String?
    var controlFD: Int32 = -1
    var inputGainDB: Double = 0.0
    init() {
        var args = Array(CommandLine.arguments.dropFirst())
        while !args.isEmpty {
            let key = args.removeFirst()
            if key == "--doctor" { doctor = true; continue }
            if key == "--locales" { locales = true; continue }
            if key == "--authorize" { authorize = true; continue }
            guard !args.isEmpty else { fatal("arguments", "Missing value for \(key)") }
            let value = args.removeFirst()
            switch key {
            case "--event-socket": eventSocket = value
            case "--engine":
                guard value == "apple" || value == "raw" else { fatal("arguments", "Unknown engine \(value)") }
                rawAudio = value == "raw"
            case "--only":
                guard value == "microphone" || value == "speech" else { fatal("arguments", "Unknown --only value") }
                only = value
            case "--input": input = value
            case "--language": language = value
            case "--endpoint-ms": endpointMS = Double(value) ?? .nan
            case "--threshold-db": thresholdDB = Double(value) ?? .nan
            case "--input-gain-db": inputGainDB = Double(value) ?? .nan
            default: fatal("arguments", "Unknown option \(key)")
            }
        }
        guard !input.isEmpty, !language.isEmpty, endpointMS.isFinite, endpointMS > 0,
              thresholdDB.isFinite, inputGainDB.isFinite, inputGainDB >= -24.0, inputGainDB <= 12.0
        else { fatal("arguments", "Invalid input, language, VAD or input-gain configuration") }
    }
}

private struct InputDevice {
    let id: AudioDeviceID
    let name: String
    let channels: Int
    let sampleRate: Double
}
private func inputDevices() -> [InputDevice] {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    let status = ids.withUnsafeMutableBytes {
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { return [] }
    return ids.compactMap { id in
        var name: CFString = "" as CFString
        address.mSelector = kAudioObjectPropertyName
        size = UInt32(MemoryLayout<CFString>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &name) == noErr else { return nil }
        address.mSelector = kAudioDevicePropertyStreamConfiguration
        address.mScope = kAudioDevicePropertyScopeInput
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr else { return nil }
        let data = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { data.deallocate() }
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, data) == noErr else { return nil }
        let channels = UnsafeMutableAudioBufferListPointer(data.assumingMemoryBound(to: AudioBufferList.self)).reduce(0) { $0 + Int($1.mNumberChannels) }
        guard channels > 0 else { return nil }
        address.mSelector = kAudioDevicePropertyNominalSampleRate
        address.mScope = kAudioObjectPropertyScopeGlobal
        var rate: Float64 = 0
        size = UInt32(MemoryLayout<Float64>.size)
        _ = AudioObjectGetPropertyData(id, &address, 0, nil, &size, &rate)
        return InputDevice(id: id, name: name as String, channels: channels, sampleRate: rate)
    }
}
private func microphoneStatus() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}
private func speechStatus() -> String {
    switch SFSpeechRecognizer.authorizationStatus() {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}
private func report(_ options: Options) {
    let recognizer = SFSpeechRecognizer(locale: Locale(identifier: options.language))
    let devices = inputDevices().filter { $0.name == options.input }
    var fields: [String: Any] = ["input": options.input, "language": options.language,
        "microphone_authorization": microphoneStatus(), "speech_authorization": speechStatus(),
        "available": recognizer?.isAvailable ?? false,
        "supports_on_device": recognizer?.supportsOnDeviceRecognition ?? false,
        "requires_on_device": true, "input_found": devices.count == 1,
        "input_matches": devices.count, "capture_started": false]
    if let device = devices.first {
        fields["device_id"] = device.id
        fields["input_channels"] = device.channels
        fields["sample_rate"] = device.sampleRate
        fields["recognition_channel"] = 0
    }
    emit("doctor", fields)
}

private final class Session {
    let id = UUID().uuidString
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    var progress = SpeechProgress()
    var ending = false
    var timeout: DispatchWorkItem?
}

private final class Capture {
    let options: Options
    let recognizer: SFSpeechRecognizer?
    var rawPending = Data()
    let engine = AVAudioEngine()
    let queue = DispatchQueue(label: "ai-voice.speech")
    var vad: VAD
    var preroll = Preroll<AVAudioPCMBuffer>(capacityMS: 300)
    var active: Session?
    var sessions: [String: Session] = [:]
    var recovery = RecognitionRecoveryPolicy()
    var signals: [DispatchSourceSignal] = []
    var gainLock = NSLock()
    var gainMultiplier: Float = 1.0

    init(_ options: Options, _ recognizer: SFSpeechRecognizer?) {
        self.options = options; self.recognizer = recognizer
        vad = VAD(endpointMS: options.endpointMS, thresholdDB: options.thresholdDB)
        gainMultiplier = Capture.linearGain(fromDB: options.inputGainDB)
    }
    static func linearGain(fromDB db: Double) -> Float {
        return Float(pow(10.0, db / 20.0))
    }
    func updateGain(db: Double) {
        let multiplier = Capture.linearGain(fromDB: db)
        gainLock.lock()
        gainMultiplier = multiplier
        gainLock.unlock()
    }
    func currentGainDB() -> Double {
        gainLock.lock()
        let m = gainMultiplier
        gainLock.unlock()
        if m <= 0 { return -120.0 }
        return 20.0 * log10(Double(m))
    }
    func handleControl(_ line: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: line, options: []),
              let dict = object as? [String: Any] else { return }
        let command = (dict["command"] as? String) ?? (dict["event"] as? String) ?? ""
        if command == "input_gain" || command == "set_gain_db" {
            guard let raw = dict["db"] ?? dict["value_db"], let number = (raw as? NSNumber)?.doubleValue else { return }
            guard number.isFinite, number >= -24.0, number <= 12.0 else { return }
            updateGain(db: number)
            let applied = currentGainDB()
            emit("input_gain", ["db": applied, "requested_db": number])
        }
    }
    func start() {
        let matches = inputDevices().filter { $0.name == options.input }
        guard matches.count == 1 else { fatal("input_device", "Expected exactly one input named \(options.input); found \(matches.count)") }
        let device = matches[0]
        let node = engine.inputNode
        guard let unit = node.audioUnit else { fatal("input_device", "Input audio unit is unavailable") }
        var deviceID = device.id
        let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global, 0, &deviceID, UInt32(MemoryLayout<AudioDeviceID>.size))
        guard status == noErr else { fatal("input_device", "Cannot select \(options.input): CoreAudio \(status)") }
        var actualID: AudioDeviceID = 0
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global, 0, &actualID, &size) == noErr, actualID == device.id else {
            fatal("input_device", "CoreAudio did not select \(options.input)")
        }
        // After CurrentDevice changes, outputFormat can keep a stale rate/channel count
        // (e.g. 44100 Hz while the device runs 96000 Hz); the tap then gets no buffers.
        let hardware = node.inputFormat(forBus: 0)
        guard hardware.channelCount > 0, hardware.sampleRate > 0 else { fatal("input_format", "Input device reports no audio format") }
        var stream = AudioStreamBasicDescription(
            mSampleRate: hardware.sampleRate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagsNativeFloatPacked | kAudioFormatFlagIsNonInterleaved,
            mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
            mChannelsPerFrame: hardware.channelCount, mBitsPerChannel: 32, mReserved: 0)
        let formatStatus = AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Output, 1,
            &stream, UInt32(MemoryLayout<AudioStreamBasicDescription>.size))
        guard formatStatus == noErr else { fatal("input_format", "Cannot set input format: CoreAudio \(formatStatus)") }
        let format = node.outputFormat(forBus: 0)
        guard format.channelCount > 0, format.sampleRate > 0,
              format.commonFormat == .pcmFormatFloat32 else { fatal("input_format", "Expected float PCM input") }
        guard let processor = FirstChannelPCM(inputFormat: format) else {
            fatal("input_format", "Cannot create first-channel PCM converter")
        }
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self, let copy = processor.copy(buffer) else { return }
            self.queue.async {
                self.gainLock.lock()
                let multiplier = self.gainMultiplier
                self.gainLock.unlock()
                if multiplier != 1.0, let samples = copy.floatChannelData?[0] {
                    let count = Int(copy.frameLength)
                    for i in 0..<count { samples[i] = max(-1.0, min(1.0, samples[i] * multiplier)) }
                }
                do { self.consume(try processor.convert(copy)) }
                catch { self.stop(); fatal("input_format", error.localizedDescription) }
            }
        }
        do { try engine.start() } catch { fatal("capture", "Cannot start \(options.input): \(error.localizedDescription)") }
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: queue)
            source.setEventHandler { [weak self] in self?.stop(); exit(0) }
            source.resume(); signals.append(source)
        }
        emit("ready", ["input": device.name, "device_id": device.id,
            "input_channels": format.channelCount, "recognition_channels": 1,
            "recognition_channel": 0, "sample_rate": format.sampleRate, "recognition_sample_rate": 16000,
            "language": options.language, "on_device": !options.rawAudio, "engine": options.rawAudio ? "raw" : "apple",
            "input_gain_db": self.currentGainDB()])
        // Fail explicitly when the selected device disappears instead of reading default input.
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            if !inputDevices().contains(where: { $0.id == device.id && $0.name == device.name }) {
                self.stop(); fatal("input_device", "Input \(device.name) was removed")
            }
        }
        timer.resume()
        deviceTimer = timer
    }
    var deviceTimer: DispatchSourceTimer?
    func stop() {
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        deviceTimer?.cancel()
        for session in sessions.values { session.timeout?.cancel(); session.task?.cancel() }
        sessions.removeAll(); active = nil; preroll.clear()
    }
    func consume(_ buffer: AVAudioPCMBuffer) {
        let duration = Double(buffer.frameLength) / buffer.format.sampleRate * 1000
        guard let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        var energy = 0.0
        for i in 0..<Int(buffer.frameLength) { energy += Double(samples[i]) * Double(samples[i]) }
        let db = 10 * log10(max(energy / Double(buffer.frameLength), 1e-12))
        if active == nil { preroll.append(buffer, durationMS: duration) }
        if options.rawAudio { consumeRaw(buffer, db: db, duration: duration); return }
        switch vad.process(db: db, durationMS: duration) {
        case .start:
            guard sessions.count < 3 else { stop(); fatal("recognition", "Too many unfinished recognition requests") }
            let session = Session(); sessions[session.id] = session; active = session
            begin(session)
            emit("vad_start", id: session.id, ["preroll_ms": 200, "buffer_ms": preroll.durationMS, "threshold_db": options.thresholdDB])
            for chunk in preroll.values { session.request?.append(chunk) }
            preroll.clear()
        case .none:
            if let session = active {
                if session.progress.completed {
                    guard session.progress.needsContinuation(db: db, thresholdDB: options.thresholdDB) else { return }
                    session.progress.beginContinuation()
                    begin(session)
                }
                session.request?.append(buffer)
            }
        case .end:
            guard let session = active else { return }
            if !session.progress.completed { session.request?.append(buffer) }
            session.ending = true; active = nil
            emit("vad_end", id: session.id)
            if session.progress.completed { finish(session) }
            else {
                session.request?.endAudio()
                let timeout = DispatchWorkItem { [weak self, weak session] in
                    guard let self, let session, self.sessions[session.id] != nil else { return }
                    self.fail(session, "Recognition did not finalize within 5 seconds")
                }
                session.timeout = timeout
                queue.asyncAfter(deadline: .now() + 5, execute: timeout)
            }
        }
    }
    /// Raw engine: stream 16 kHz mono Int16 PCM per utterance; recognition happens in Python.
    func consumeRaw(_ buffer: AVAudioPCMBuffer, db: Double, duration: Double) {
        switch vad.process(db: db, durationMS: duration) {
        case .start:
            let session = Session(); active = session
            emit("vad_start", id: session.id, ["preroll_ms": 200, "buffer_ms": preroll.durationMS, "threshold_db": options.thresholdDB])
            for chunk in preroll.values { appendRaw(chunk, session) }
            preroll.clear()
        case .none:
            if let session = active { appendRaw(buffer, session) }
        case .end:
            guard let session = active else { return }
            appendRaw(buffer, session)
            flushRaw(session)
            active = nil
            emit("vad_end", id: session.id)
        }
    }
    func appendRaw(_ buffer: AVAudioPCMBuffer, _ session: Session) {
        guard let samples = buffer.floatChannelData?[0] else { return }
        for i in 0..<Int(buffer.frameLength) {
            var value = Int16(max(-1.0, min(1.0, samples[i])) * 32767).littleEndian
            withUnsafeBytes(of: &value) { rawPending.append(contentsOf: $0) }
        }
        if rawPending.count >= 16000 { flushRaw(session) }  // keeps each event far below the protocol line limit
    }
    func flushRaw(_ session: Session) {
        guard !rawPending.isEmpty else { return }
        emit("audio", id: session.id, ["pcm": rawPending.base64EncodedString()])
        rawPending.removeAll(keepingCapacity: true)
    }
    func begin(_ session: Session) {
        guard let recognizer else { return }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true
        request.taskHint = .dictation
        session.request = request
        session.task = recognizer.recognitionTask(with: request) { [weak self, weak session] result, error in
            guard let self, let session else { return }
            self.queue.async {
                guard self.sessions[session.id] != nil, session.request === request else { return }
                if let result {
                    session.progress.receive(result.bestTranscription.formattedString, isFinal: result.isFinal)
                    if result.isFinal {
                        if session.ending { self.finish(session) }
                    } else {
                        emit("partial", id: session.id, ["text": session.progress.text])
                    }
                }
                if let error, self.sessions[session.id] != nil {
                    let nativeError = error as NSError
                    self.fail(session, nativeError.localizedDescription, nativeDomain: nativeError.domain, nativeCode: nativeError.code)
                }
            }
        }
    }
    func finish(_ session: Session) {
        guard let text = session.progress.claimFinal() else { return }
        session.timeout?.cancel()
        emit("final", id: session.id, ["text": text])
        session.request = nil; session.task = nil
        sessions.removeValue(forKey: session.id)
        recovery.finalized()
    }
    func fail(_ session: Session, _ message: String, nativeDomain: String = "AI Voice", nativeCode: Int = 1) {
        session.timeout?.cancel(); session.task?.cancel(); session.request = nil
        let noSpeech = RecognitionRecoveryPolicy.isNoSpeech(domain: nativeDomain, code: nativeCode)
        let terminal = recovery.failure(domain: nativeDomain, code: nativeCode, available: recognizer?.isAvailable ?? true)
        emit("error", id: session.id, ["code": noSpeech ? "no_speech" : "recognition", "message": message,
            "fatal": terminal, "native_domain": nativeDomain, "native_code": nativeCode])
        if terminal { stop(); exit(1) }
        // Keep only a previously finalized segment; an unfinished continuation is discarded.
        if session.progress.restoreCompletedPrefix() {
            if session.ending { finish(session) }
        } else {
            sessions.removeValue(forKey: session.id)
            if active === session { active = nil; vad.reset() }
        }
    }

}

private final class ControlReader {
    private let handle: FileHandle
    private let capture: Capture
    init(handle: FileHandle, capture: Capture) {
        self.handle = handle; self.capture = capture
    }
    func start() {
        var buffer = Data()
        let maxLine = 64 * 1024
        while true {
            let chunk: Data
            do { chunk = try handle.read(upToCount: 4096) ?? Data() }
            catch { break }
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer.subdata(in: buffer.startIndex..<newline)
                buffer.removeSubrange(buffer.startIndex...newline)
                guard line.count <= maxLine else {
                    capture.queue.sync { capture.stop() }
                    exit(1)
                }
                let trimmed = line.drop(while: { $0 == 0x20 || $0 == 0x09 || $0 == 0x0D })
                if trimmed.isEmpty { continue }
                capture.queue.sync { capture.handleControl(Data(trimmed)) }
            }
            if buffer.count > maxLine {
                capture.queue.sync { capture.stop() }
                exit(1)
            }
        }
        capture.queue.sync { capture.stop() }
        exit(0)
    }
}

@main private struct SpeechHelper {
    static var capture: Capture?
    static func main() {
        var options = Options()
        if let path = options.eventSocket {
            options.controlFD = connectEvents(path)
        }
        if options.doctor { report(options); return }
        if options.locales {
            emit("locales", ["system": Locale.current.identifier.replacingOccurrences(of: "_", with: "-"),
                             "supported": SFSpeechRecognizer.supportedLocales().map {
                                 $0.identifier.replacingOccurrences(of: "_", with: "-")
                             }.sorted()])
            exit(0)
        }
        if let only = options.only {
            guard options.authorize else { fatal("arguments", "--only requires --authorize") }
            // Request just one permission, print the doctor report and exit; denial is not an error.
            let finish = { DispatchQueue.main.async { report(options); exit(0) } }
            if only == "microphone" {
                if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
                    AVCaptureDevice.requestAccess(for: .audio) { _ in finish() }
                } else { report(options); exit(0) }
            } else {
                if SFSpeechRecognizer.authorizationStatus() == .notDetermined {
                    SFSpeechRecognizer.requestAuthorization { _ in finish() }
                } else { report(options); exit(0) }
            }
            RunLoop.main.run()
            return
        }
        // Permission requests run on the main run loop and are owned by the signed app.
        authorizeMicrophone {
            if options.rawAudio {
                if options.authorize { report(options); exit(0) }
                startCapture(options, nil)
                return
            }
            SFSpeechRecognizer.requestAuthorization { status in
                DispatchQueue.main.async {
                    guard status == .authorized else { fatal("speech_permission", "Speech permission is \(speechStatus()); allow AI Voice Speech Helper in System Settings") }
                    if options.authorize { report(options); exit(0) }
                    guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: options.language)),
                          recognizer.isAvailable, recognizer.supportsOnDeviceRecognition else {
                        fatal("on_device_unavailable", "On-device \(options.language) speech recognition is unavailable")
                    }
                    startCapture(options, recognizer)
                }
            }
        }
        RunLoop.main.run()
    }
    static func startCapture(_ options: Options, _ recognizer: SFSpeechRecognizer?) {
        let owner = Capture(options, recognizer); capture = owner; owner.start()
        guard options.controlFD >= 0 else { fatal("ipc", "Event socket is required") }
        let fd = dup(options.controlFD)
        guard fd >= 0 else { fatal("ipc", "Cannot dup control fd") }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        DispatchQueue.global().async { ControlReader(handle: handle, capture: owner).start() }
    }
    static func connectEvents(_ path: String) -> Int32 {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            fatal("ipc", "Event socket path is too long")
        }
        withUnsafeMutableBytes(of: &address.sun_path) { dest in
            bytes.withUnsafeBytes { source in dest.copyBytes(from: source) }
        }
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { fatal("ipc", "Cannot create event socket") }
        let status = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard status == 0, dup2(fd, STDOUT_FILENO) >= 0 else {
            fatal("ipc", "Cannot connect private event socket")
        }
        signal(SIGPIPE, SIG_IGN)
        emit("hello", ["pid": Int(getpid())])
        return fd
    }
    static func authorizeMicrophone(_ completion: @escaping () -> Void) {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .authorized { completion(); return }
        AVCaptureDevice.requestAccess(for: .audio) { allowed in
            DispatchQueue.main.async {
                guard allowed else { fatal("microphone_permission", "Microphone permission is \(microphoneStatus()); allow AI Voice Speech Helper in System Settings") }
                completion()
            }
        }
    }
}
