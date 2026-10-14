// Local text-line OCR for flat images (PNG or JPG). Apple Vision, no network.
// Usage: swift ocr_lines.swift IMAGE         -> JSON on stdout: {width, height, lines: [{text, confidence, rect: [x, y, w, h]}]}
//        swift ocr_lines.swift IMAGE IMAGE... -> {"engine", "frames": [{file, width, height, lines}]} (one start-up for many frames)
// Rects are pixels with a top-left origin. Language correction is off so visible words are not normalized.
import Foundation
import Vision
import ImageIO

let paths = Array(CommandLine.arguments.dropFirst())
guard !paths.isEmpty else {
    FileHandle.standardError.write(Data("Usage: swift ocr_lines.swift IMAGE [IMAGE...]\n".utf8)); exit(2)
}
let engine = "Apple Vision VNRecognizeTextRequest (accurate, no language correction)"

func read(_ path: String) throws -> [String: Any] {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        FileHandle.standardError.write(Data("Could not decode image: \(path)\n".utf8)); exit(2)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0
    try VNImageRequestHandler(cgImage: image, orientation: .up, options: [:]).perform([request])
    let w = Double(image.width), h = Double(image.height)
    var lines: [[String: Any]] = []
    for observation in request.results ?? [] {
        guard let best = observation.topCandidates(1).first else { continue }
        let b = observation.boundingBox
        let x: Double = Double(b.minX) * w
        let y: Double = (1.0 - Double(b.maxY)) * h
        let bw: Double = Double(b.width) * w
        let bh: Double = Double(b.height) * h
        let rect: [Double] = [x, y, bw, bh].map { (v: Double) -> Double in (v * 10).rounded() / 10 }
        lines.append(["text": best.string, "confidence": Double(best.confidence), "rect": rect])
    }
    return ["file": path, "width": image.width, "height": image.height, "lines": lines]
}

var out: [String: Any]
if paths.count == 1 {
    out = try read(paths[0])
    out["engine"] = engine
    out.removeValue(forKey: "file")
} else {
    var frames: [[String: Any]] = []
    for path in paths { try autoreleasepool { frames.append(try read(path)) } }
    out = ["engine": engine, "frames": frames]
}
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys]))
FileHandle.standardOutput.write(Data("\n".utf8))
