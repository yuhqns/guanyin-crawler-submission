// 用 macOS 內建的 Apple Vision 批次辨識一個資料夾裡的字幕圖片。
// 用法：subtitle_ocr <圖片資料夾>  → 標準輸出，每行一筆（以 tab 分隔）：
//   檔名  信心  左緣  右緣  上緣  字高  文字     （位置都是 0–1，相對於圖片）
import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: subtitle_ocr <image_dir>\n".data(using: .utf8)!)
    exit(2)
}
let dir = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [])
    .filter { $0.hasSuffix(".jpg") || $0.hasSuffix(".png") }
    .sorted()

for name in names {
    autoreleasepool {
        let url = dir.appendingPathComponent(name)
        guard let image = NSImage(contentsOf: url),
              let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
            print("\(name)\t-1\t0\t0\t0\t0\t")
            return
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLanguages = ["zh-Hant"]
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(cgImage: cg).perform([request])
        } catch {
            print("\(name)\t-1\t0\t0\t0\t0\t")
            return
        }
        for item in request.results ?? [] {
            guard let best = item.topCandidates(1).first else { continue }
            let box = item.boundingBox
            let text = best.string.replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\n", with: " ")
            print(String(format: "%@\t%.2f\t%.3f\t%.3f\t%.3f\t%.3f\t%@",
                         name, best.confidence, box.minX, box.maxX, 1 - box.maxY, box.height, text))
        }
    }
}
