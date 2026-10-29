// SPDX-License-Identifier: AGPL-3.0-or-later
// Vehicle type and colour from Apple's on-device foundation model (macOS 27+), for
// piport/apple_fm.py. It prints "ready" (or "unavailable: <reason>") once, then reads one
// image path per line on stdin and answers each with one JSON line on stdout:
// {"answer": "suv black", "ms": 1234} or {"error": "..."}.
import Foundation
import FoundationModels

let instructions = """
You label vehicles in security-camera crops. Answer with exactly two lowercase words \
separated by a space: the vehicle type, one of car suv truck van bus motorcycle bike trailer, \
then its body colour, one of red orange yellow green blue purple pink brown black white gray. \
A sedan, hatchback, wagon or coupe is car; a pickup is truck; a camper, RV or towed trailer \
is trailer. Nothing else.
"""

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

@main
struct VehicleAttrs {
    static func main() async {
        let model = SystemLanguageModel.default
        guard case .available = model.availability else {
            print("unavailable: \(model.availability)")
            fflush(stdout)
            exit(2)
        }
        print("ready")
        fflush(stdout)
        while let path = readLine(strippingNewline: true) {
            if path.isEmpty { continue }
            // A fresh session per image: earlier answers must not colour the next one.
            let session = LanguageModelSession(instructions: instructions)
            let started = Date()
            do {
                // Greedy: the same crop always gets the same answer (sampled, a far SUV
                // came back brown on one run and blue on the next).
                let reply = try await session.respond(
                    options: GenerationOptions(samplingMode: .greedy, maximumResponseTokens: 12)
                ) {
                    "What type and colour is this vehicle?"
                    Attachment(imageURL: URL(fileURLWithPath: path))
                }
                emit(["answer": reply.content.trimmingCharacters(in: .whitespacesAndNewlines),
                      "ms": Int(Date().timeIntervalSince(started) * 1000)])
            } catch {
                emit(["error": "\(error)"])
            }
        }
    }
}
