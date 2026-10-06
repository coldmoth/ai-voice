import Foundation

enum VADEvent: Equatable {
    case none, start(prerollMS: Double), end
}

/// Duration-based pure state machine. Audio storage belongs to the capture owner.
struct VAD {
    let endpointMS: Double
    let prerollMS: Double
    let minSpeechMS: Double
    let thresholdDB: Double
    private var active = false
    private var candidateMS: Double = 0
    private var silentMS: Double = 0

    init(endpointMS: Double = 450, prerollMS: Double = 200,
         minSpeechMS: Double = 100, thresholdDB: Double = -45) {
        self.endpointMS = endpointMS
        self.prerollMS = prerollMS
        self.minSpeechMS = minSpeechMS
        self.thresholdDB = thresholdDB
    }
    mutating func reset() {
        active = false
        candidateMS = 0
        silentMS = 0
    }
    mutating func process(db: Double, durationMS: Double) -> VADEvent {
        if db >= thresholdDB {
            silentMS = 0
            if !active {
                candidateMS += durationMS
                if candidateMS >= minSpeechMS {
                    active = true
                    return .start(prerollMS: prerollMS)
                }
            }
        } else if active {
            silentMS += durationMS
            if silentMS >= endpointMS {
                reset()
                return .end
            }
        } else {
            candidateMS = 0
        }
        return .none
    }
}

struct Preroll<Value> {
    let capacityMS: Double
    private var entries: [(value: Value, duration: Double)] = []
    private(set) var durationMS = 0.0
    var values: [Value] { entries.map { $0.value } }
    mutating func append(_ value: Value, durationMS: Double) {
        entries.append((value, durationMS))
        self.durationMS += durationMS
        while entries.count > 1, self.durationMS - entries[0].duration >= capacityMS {
            self.durationMS -= entries.removeFirst().duration
        }
    }
    mutating func clear() { entries.removeAll(); durationMS = 0 }
}

/// Holds completed segments until VAD ends the utterance; silence never restarts Speech.
struct SpeechProgress {
    private(set) var text = ""
    private var prefix = ""
    private(set) var completed = false
    private var emitted = false
    func needsContinuation(db: Double, thresholdDB: Double) -> Bool {
        completed && !emitted && db >= thresholdDB
    }
    mutating func beginContinuation() {
        prefix = text
        completed = false
    }
    mutating func receive(_ segment: String, isFinal: Bool) {
        text = [prefix, segment].filter { !$0.isEmpty }.joined(separator: " ")
        completed = isFinal
    }
    mutating func claimFinal() -> String? {
        guard completed, !emitted else { return nil }
        emitted = true
        return text
    }
    mutating func restoreCompletedPrefix() -> Bool {
        guard !prefix.isEmpty else { return false }
        text = prefix
        completed = true
        return true
    }
}

/// Apple documents assistant-domain 1110 as no recognized speech, not service failure.
struct RecognitionRecoveryPolicy {
    private(set) var consecutiveFailures = 0
    static func isNoSpeech(domain: String, code: Int) -> Bool {
        domain == "kAFAssistantErrorDomain" && code == 1110
    }
    mutating func failure(domain: String, code: Int, available: Bool) -> Bool {
        guard available else { return true }
        if Self.isNoSpeech(domain: domain, code: code) { return false }
        consecutiveFailures += 1
        return consecutiveFailures >= 3
    }
    mutating func finalized() { consecutiveFailures = 0 }
}
