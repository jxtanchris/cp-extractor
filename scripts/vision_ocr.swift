import Foundation
import Vision
import AppKit
import PDFKit

struct TextBox {
    let text: String
    let minX: CGFloat
    let midY: CGFloat
    let height: CGFloat
}

func recognizeCGImage(_ cgImg: CGImage) -> String {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    req.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cgImg, options: [:])
    do {
        try handler.perform([req])
    } catch {
        return ""
    }

    guard let results = req.results, !results.isEmpty else {
        return ""
    }

    var boxes: [TextBox] = []
    for obs in results {
        guard let top = obs.topCandidates(1).first else { continue }
        let bbox = obs.boundingBox // Vision coordinates: origin at bottom-left, normalized 0..1
        boxes.append(TextBox(
            text: top.string.trimmingCharacters(in: .whitespacesAndNewlines),
            minX: bbox.minX,
            midY: bbox.midY,
            height: bbox.height
        ))
    }

    // Sort from top to bottom (descending midY in Vision coordinates)
    boxes.sort { $0.midY > $1.midY }

    // Group boxes into horizontal lines if their vertical centers are close
    var lines: [[TextBox]] = []
    for box in boxes where !box.text.isEmpty {
        if var lastLine = lines.last, let firstInLine = lastLine.first {
            let threshold = max(firstInLine.height, box.height) * 0.55
            if abs(firstInLine.midY - box.midY) <= threshold {
                lastLine.append(box)
                lines[lines.count - 1] = lastLine
                continue
            }
        }
        lines.append([box])
    }

    var outputLines: [String] = []
    for line in lines {
        let sortedLine = line.sorted { $0.minX < $1.minX }
        if sortedLine.count > 1 {
            outputLines.append(sortedLine.map { $0.text }.joined(separator: " | "))
        } else if let single = sortedLine.first {
            outputLines.append(single.text)
        }
    }

    return outputLines.joined(separator: "\n")
}

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: vision_ocr <file_path>\n", stderr)
    exit(1)
}

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)
let ext = url.pathExtension.lowercased()

if ext == "pdf" {
    guard let pdf = PDFDocument(url: url) else {
        fputs("Failed to open PDF\n", stderr)
        exit(2)
    }
    var pageTexts: [String] = []
    for i in 0..<pdf.pageCount {
        guard let page = pdf.page(at: i) else { continue }
        let pageRect = page.bounds(for: .mediaBox)
        let scale: CGFloat = 2.0
        let width = Int(max(pageRect.width * scale, 100))
        let height = Int(max(pageRect.height * scale, 100))
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { continue }

        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        ctx.saveGState()
        ctx.scaleBy(x: scale, y: scale)
        page.draw(with: .mediaBox, to: ctx)
        ctx.restoreGState()

        if let cgImg = ctx.makeImage() {
            let txt = recognizeCGImage(cgImg)
            if !txt.isEmpty {
                pageTexts.append("<!-- 第 \(i + 1) 页 (OCR) -->\n\(txt)")
            }
        }
    }
    print(pageTexts.joined(separator: "\n\n"))
} else {
    guard let img = NSImage(contentsOf: url),
          let cgImg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fputs("Failed to load image\n", stderr)
        exit(2)
    }
    print(recognizeCGImage(cgImg))
}
