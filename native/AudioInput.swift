import AVFoundation
import CoreAudio

/// Copy only the selected first channel on the tap thread; convert on the serial ASR queue.
/// Gain multiplier is owned by Capture and applied here on the ASR serial queue so the
/// capture-owned multiplier is the single source of truth.
final class FirstChannelPCM {
    let mono: AVAudioFormat
    let speech: AVAudioFormat
    let converter: AVAudioConverter
    let gainLock: NSLock
    var gain: Float

    init?(inputFormat: AVAudioFormat) {
        guard inputFormat.commonFormat == .pcmFormatFloat32, inputFormat.channelCount > 0,
              let mono = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inputFormat.sampleRate, channels: 1, interleaved: false),
              let speech = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: mono, to: speech) else { return nil }
        self.mono = mono; self.speech = speech; self.converter = converter
        self.gainLock = NSLock()
        self.gain = 1.0
    }
    func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let source = buffer.floatChannelData?[0],
              let copy = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: buffer.frameLength),
              let target = copy.floatChannelData?[0] else { return nil }
        copy.frameLength = buffer.frameLength
        if buffer.format.isInterleaved {
            for i in 0..<Int(buffer.frameLength) { target[i] = source[i * Int(buffer.format.channelCount)] }
        } else { target.update(from: source, count: Int(buffer.frameLength)) }
        return copy
    }
    func convert(_ buffer: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        let frames = AVAudioFrameCount(ceil(Double(buffer.frameLength) * speech.sampleRate / mono.sampleRate) + 64)
        let output = AVAudioPCMBuffer(pcmFormat: speech, frameCapacity: frames)!
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, inputStatus in
            if supplied { inputStatus.pointee = .noDataNow; return nil }
            supplied = true; inputStatus.pointee = .haveData; return buffer
        }
        if let error { throw error }
        if status == .error { throw NSError(domain: "AI Voice PCM", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot convert microphone audio to 16 kHz mono"]) }
        return output
    }
}
