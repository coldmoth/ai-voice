import Foundation
import AVFoundation

@main struct PCMTests {
    static func main() throws {
        let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 4)!
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 96000, interleaved: false, channelLayout: layout)
        let source = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 9600)!
        source.frameLength = 9600
        for i in 0..<9600 {
            source.floatChannelData![0][i] = Float(sin(Double(i) * 2 * .pi * 1000 / 96000)) * 0.25
            for channel in 1..<4 { source.floatChannelData![channel][i] = 0.9 }
        }
        let processor = FirstChannelPCM(inputFormat: format)!
        let copied = processor.copy(source)!
        source.floatChannelData![0][100] = 9 // prove ownership after tap returns
        assert(copied.floatChannelData![0][100] < 0.3, "copy must own samples")
        let output = try processor.convert(copied)
        assert(output.format.channelCount == 1 && output.format.sampleRate == 16000)
        assert(output.frameLength > 1400 && output.frameLength <= 1600)
        var energy = 0.0
        for i in 0..<Int(output.frameLength) { let x = Double(output.floatChannelData![0][i]); energy += x * x }
        let rms = sqrt(energy / Double(output.frameLength))
        assert(rms > 0.14 && rms < 0.20, "only channel 0 reaches recognizer")
        print("PCM ownership, mono channel 0 and 96k→16k assertions passed")
    }
}
