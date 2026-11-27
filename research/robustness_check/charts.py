"""High-DPI Traditional Chinese PNG chart renderer via macOS Swift CoreGraphics (PingFang TC).

Renders 4 required charts into `research/robustness_check/`:
  1. verdict_heatmap.png          — 判定總表熱力圖（R1–R5 × 八關 + 總判定）
  2. intl_crash_equity_curves.png — 各國崩盤情境下 R1–R4 的資產曲線（日本 1989 20年、上證 2007 10年、德英 2000 10年）
  3. behavior_xirr_loss_bars.png  — 行為錯誤造成的 XIRR 損失長條圖（情境 A/B/C × R1–R3）
  4. withdrawal_ruin_prob.png     — 提領期資金耗盡機率與 30 年存活曲線圖
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SWIFT_RENDERER_SOURCE = r'''
import Foundation
import AppKit
import CoreGraphics

func makeCanvas(width: Int, height: Int) -> (NSBitmapImageRep, CGContext) {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    )!
    let nsCtx = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = nsCtx
    let ctx = nsCtx.cgContext
    ctx.setFillColor(NSColor(calibratedRed: 0.985, green: 0.988, blue: 0.995, alpha: 1.0).cgColor)
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    return (rep, ctx)
}

func drawStr(_ text: String, x: CGFloat, y: CGFloat, size: CGFloat, bold: Bool = false, color: NSColor = .black, align: String = "left") {
    let fontName = bold ? "PingFangTC-Semibold" : "PingFangTC-Regular"
    let font = NSFont(name: fontName, size: size) ?? NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
    let ns = NSString(string: text)
    let sz = ns.size(withAttributes: attrs)
    var dx = x
    if align == "center" { dx = x - sz.width / 2.0 }
    else if align == "right" { dx = x - sz.width }
    ns.draw(at: NSPoint(x: dx, y: y), withAttributes: attrs)
}

func colorFromHex(_ hex: String) -> NSColor {
    var s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.hasPrefix("#") { s.removeFirst() }
    var rgb: UInt64 = 0
    Scanner(string: s).scanHexInt64(&rgb)
    let r = CGFloat((rgb >> 16) & 0xFF) / 255.0
    let g = CGFloat((rgb >> 8) & 0xFF) / 255.0
    let b = CGFloat(rgb & 0xFF) / 255.0
    return NSColor(calibratedRed: r, green: g, blue: b, alpha: 1.0)
}

func savePNG(_ rep: NSBitmapImageRep, _ path: String) throws {
    let data = rep.representation(using: .png, properties: [:])!
    try data.write(to: URL(fileURLWithPath: path))
}

// 1) Verdict Heatmap
struct HeatCell: Codable {
    let status: String
    let detail: String
}
struct HeatRow: Codable {
    let recId: String
    let recLabel: String
    let cells: [HeatCell]
}
struct HeatmapPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let colHeaders: [String]
    let rows: [HeatRow]
}

// 2) Multi-panel Curves (Used for Intl Crashes & Survival Curves)
struct CurveLine: Codable {
    let label: String
    let values: [Double]
    let colorHex: String
    let dashed: Bool
    let lineWidth: Double
}
struct CurvePanel: Codable {
    let title: String
    let subtitle: String
    let xLabels: [String]
    let xIndices: [Int]
    let yMin: Double
    let yMax: Double
    let yStep: Double
    let yUnit: String
    let lines: [CurveLine]
}
struct MultiCurvePayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let panels: [CurvePanel]
}

// 3) Grouped Bar Chart (Used for Behavior XIRR Loss & Withdrawal Ruin Prob)
struct BarSeries: Codable {
    let name: String
    let colorHex: String
    let values: [Double]
}
struct GroupedBarPanel: Codable {
    let title: String
    let subtitle: String
    let categories: [String]
    let yMin: Double
    let yMax: Double
    let yStep: Double
    let yUnit: String
    let series: [BarSeries]
}
struct GroupedBarPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let footnote: String
    let panels: [GroupedBarPanel]
}

// 4) Combined Withdrawal Chart (Left: Grouped Ruin Bars, Right: 30Y Survival Curves)
struct WithdrawalPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let footnote: String
    let barPanel: GroupedBarPanel
    let curvePanel: CurvePanel
}

let mode = CommandLine.arguments[1]
let jsonPath = CommandLine.arguments[2]
let rawData = try Data(contentsOf: URL(fileURLWithPath: jsonPath))

if mode == "heatmap" {
    let p = try JSONDecoder().decode(HeatmapPayload.self, from: rawData)
    let w = 2560
    let h = 1360
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)

    let padL: CGFloat = 460
    let padR: CGFloat = 65
    let padB: CGFloat = 140
    let padT: CGFloat = 235
    let gridW = CGFloat(w) - padL - padR
    let gridH = CGFloat(h) - padT - padB

    let nCols = p.colHeaders.count
    let nRows = p.rows.count
    let colW = gridW / CGFloat(nCols)
    let rowH = gridH / CGFloat(nRows)

    // Column headers
    for c in 0..<nCols {
        let cx = padL + (CGFloat(c) + 0.5) * colW
        let isFinal = (c == nCols - 1)
        let hdrParts = p.colHeaders[c].components(separatedBy: "\n")
        if hdrParts.count >= 2 {
            drawStr(hdrParts[0], x: cx, y: CGFloat(h) - padT + 44, size: isFinal ? 24 : 21, bold: true,
                    color: isFinal ? colorFromHex("#1E293B") : colorFromHex("#334155"), align: "center")
            drawStr(hdrParts[1], x: cx, y: CGFloat(h) - padT + 14, size: 18, bold: isFinal,
                    color: colorFromHex("#475569"), align: "center")
        } else {
            drawStr(p.colHeaders[c], x: cx, y: CGFloat(h) - padT + 26, size: 22, bold: true, color: colorFromHex("#1E293B"), align: "center")
        }
    }

    for (rIdx, row) in p.rows.enumerated() {
        let ry = CGFloat(h) - padT - CGFloat(rIdx + 1) * rowH
        // Row label on left
        let lblParts = row.recLabel.components(separatedBy: "\n")
        drawStr(lblParts[0], x: padL - 22, y: ry + rowH * 0.52, size: 23, bold: true, color: colorFromHex("#0F172A"), align: "right")
        if lblParts.count >= 2 {
            drawStr(lblParts[1], x: padL - 22, y: ry + rowH * 0.22, size: 17.5, bold: false, color: colorFromHex("#475569"), align: "right")
        }

        for (cIdx, cell) in row.cells.enumerated() {
            let cx0 = padL + CGFloat(cIdx) * colW
            let isFinal = (cIdx == nCols - 1)
            let inset: CGFloat = isFinal ? 6.0 : 5.0
            let cellRect = CGRect(x: cx0 + inset, y: ry + inset, width: colW - 2 * inset, height: rowH - 2 * inset)

            let bgCol: NSColor
            let fgCol: NSColor
            let borderCol: NSColor
            if cell.status == "通過" || cell.status == "站得住" {
                bgCol = isFinal ? colorFromHex("#15803D") : colorFromHex("#DCFCE7")
                fgCol = isFinal ? NSColor.white : colorFromHex("#14532D")
                borderCol = colorFromHex("#22C55E")
            } else if cell.status == "有條件通過" || cell.status == "有條件成立" {
                bgCol = isFinal ? colorFromHex("#D97706") : colorFromHex("#FEF3C7")
                fgCol = isFinal ? NSColor.white : colorFromHex("#78350F")
                borderCol = colorFromHex("#F59E0B")
            } else {
                bgCol = isFinal ? colorFromHex("#B91C1C") : colorFromHex("#FEE2E2")
                fgCol = isFinal ? NSColor.white : colorFromHex("#7F1D1D")
                borderCol = colorFromHex("#EF4444")
            }

            let cPath = CGPath(roundedRect: cellRect, cornerWidth: 10, cornerHeight: 10, transform: nil)
            ctx.setFillColor(bgCol.cgColor)
            ctx.addPath(cPath)
            ctx.fillPath()
            ctx.setStrokeColor(borderCol.cgColor)
            ctx.setLineWidth(isFinal ? 3.2 : 2.0)
            ctx.addPath(cPath)
            ctx.strokePath()

            drawStr(cell.status, x: cellRect.midX, y: cellRect.minY + cellRect.height * 0.50,
                    size: isFinal ? 25 : 22, bold: true, color: fgCol, align: "center")
            drawStr(cell.detail, x: cellRect.midX, y: cellRect.minY + cellRect.height * 0.17,
                    size: 15.5, bold: false, color: isFinal ? NSColor(calibratedWhite: 0.95, alpha: 1.0) : fgCol.withAlphaComponent(0.88), align: "center")
        }
    }

    drawStr(p.title, x: 65, y: CGFloat(h) - 78, size: 38, bold: true, color: colorFromHex("#0F172A"))
    drawStr(p.subtitle, x: 65, y: CGFloat(h) - 124, size: 22, bold: false, color: colorFromHex("#475569"))

    // Bottom legend
    let leg: [(String, String, String)] = [
        ("通過 / 站得住", "#DCFCE7", "#15803D"),
        ("有條件通過 / 有條件成立（須滿足附帶條件）", "#FEF3C7", "#B45309"),
        ("不通過 / 被推翻（須改用替代方案）", "#FEE2E2", "#B91C1C")
    ]
    var lx: CGFloat = padL
    let ly: CGFloat = 45
    for (txt, bgH, fgH) in leg {
        let r = CGRect(x: lx, y: ly, width: 32, height: 28)
        ctx.setFillColor(colorFromHex(bgH).cgColor)
        ctx.fill(r)
        ctx.setStrokeColor(colorFromHex(fgH).cgColor)
        ctx.setLineWidth(2.0)
        ctx.stroke(r)
        drawStr(txt, x: lx + 42, y: ly - 1, size: 20, bold: true, color: colorFromHex(fgH))
        lx += 630
    }

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)

} else if mode == "multicurve" {
    let p = try JSONDecoder().decode(MultiCurvePayload.self, from: rawData)
    let w = 2560
    let h = 1480
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)

    let nP = p.panels.count
    let padL: CGFloat = 125
    let padR: CGFloat = 55
    let padB: CGFloat = 205
    let padT: CGFloat = 215
    let gap: CGFloat = 95
    let panelW = (CGFloat(w) - padL - padR - CGFloat(nP - 1) * gap) / CGFloat(nP)
    let panelH = CGFloat(h) - padT - padB

    for (pIdx, panel) in p.panels.enumerated() {
        let px0 = padL + CGFloat(pIdx) * (panelW + gap)
        let prect = CGRect(x: px0, y: padB, width: panelW, height: panelH)
        ctx.setFillColor(NSColor.white.cgColor)
        ctx.fill(prect)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.80, alpha: 1.0).cgColor)
        ctx.setLineWidth(2.0)
        ctx.stroke(prect)

        let nPts = panel.lines[0].values.count
        func mapX(_ idx: Int) -> CGFloat {
            return prect.minX + (CGFloat(idx) / CGFloat(max(1, nPts - 1))) * prect.width
        }
        func mapY(_ v: Double) -> CGFloat {
            let t = (v - panel.yMin) / (panel.yMax - panel.yMin)
            return prect.minY + CGFloat(t) * prect.height
        }

        var yv = panel.yMin
        while yv <= panel.yMax + 0.01 {
            let py = mapY(yv)
            ctx.setStrokeColor(NSColor(calibratedWhite: 0.91, alpha: 1.0).cgColor)
            ctx.setLineWidth(1.4)
            ctx.move(to: CGPoint(x: prect.minX, y: py))
            ctx.addLine(to: CGPoint(x: prect.maxX, y: py))
            ctx.strokePath()
            drawStr(String(format: "%.0f", yv) + panel.yUnit, x: prect.minX - 10, y: py - 12, size: 19, bold: false, color: colorFromHex("#334155"), align: "right")
            yv += panel.yStep
        }

        for k in 0..<panel.xIndices.count {
            let xi = panel.xIndices[k]
            let px = mapX(xi)
            ctx.setStrokeColor(NSColor(calibratedWhite: 0.92, alpha: 1.0).cgColor)
            ctx.setLineWidth(1.2)
            ctx.move(to: CGPoint(x: px, y: prect.minY))
            ctx.addLine(to: CGPoint(x: px, y: prect.maxY))
            ctx.strokePath()
            drawStr(panel.xLabels[k], x: px, y: prect.minY - 36, size: 18, bold: false, color: colorFromHex("#334155"), align: "center")
        }

        drawStr(panel.title, x: prect.minX, y: prect.maxY + 38, size: 24, bold: true, color: colorFromHex("#0F172A"))
        drawStr(panel.subtitle, x: prect.minX, y: prect.maxY + 9, size: 17.5, bold: false, color: colorFromHex("#475569"))

        ctx.saveGState()
        ctx.clip(to: prect)
        for ln in panel.lines {
            ctx.saveGState()
            ctx.setStrokeColor(colorFromHex(ln.colorHex).cgColor)
            ctx.setLineWidth(CGFloat(ln.lineWidth))
            if ln.dashed { ctx.setLineDash(phase: 0, lengths: [10, 7]) }
            ctx.move(to: CGPoint(x: mapX(0), y: mapY(ln.values[0])))
            for i in 1..<ln.values.count {
                ctx.addLine(to: CGPoint(x: mapX(i), y: mapY(ln.values[i])))
            }
            ctx.strokePath()
            ctx.restoreGState()
        }
        ctx.restoreGState()
    }

    // Bottom legend (2 rows of 3)
    let lines0 = p.panels[0].lines
    var lx: CGFloat = padL
    var ly: CGFloat = 86
    for (idx, ln) in lines0.enumerated() {
        if idx == 3 {
            lx = padL
            ly = 38
        }
        ctx.saveGState()
        ctx.setStrokeColor(colorFromHex(ln.colorHex).cgColor)
        ctx.setLineWidth(4.5)
        if ln.dashed { ctx.setLineDash(phase: 0, lengths: [8, 5]) }
        ctx.move(to: CGPoint(x: lx, y: ly + 12))
        ctx.addLine(to: CGPoint(x: lx + 44, y: ly + 12))
        ctx.strokePath()
        ctx.restoreGState()
        drawStr(ln.label, x: lx + 54, y: ly, size: 20, bold: true, color: colorFromHex(ln.colorHex))
        lx += 780
    }

    drawStr(p.title, x: padL, y: CGFloat(h) - 76, size: 36, bold: true, color: colorFromHex("#0F172A"))
    drawStr(p.subtitle, x: padL, y: CGFloat(h) - 120, size: 21, bold: false, color: colorFromHex("#475569"))

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)

} else if mode == "groupedbars" {
    let p = try JSONDecoder().decode(GroupedBarPayload.self, from: rawData)
    let w = 2480
    let h = 1440
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)

    let nP = p.panels.count
    let padL: CGFloat = 145
    let padR: CGFloat = 65
    let padB: CGFloat = 215
    let padT: CGFloat = 215
    let gap: CGFloat = 110
    let panelW = (CGFloat(w) - padL - padR - CGFloat(max(0, nP - 1)) * gap) / CGFloat(nP)
    let panelH = CGFloat(h) - padT - padB

    for (pIdx, panel) in p.panels.enumerated() {
        let px0 = padL + CGFloat(pIdx) * (panelW + gap)
        let prect = CGRect(x: px0, y: padB, width: panelW, height: panelH)
        ctx.setFillColor(NSColor.white.cgColor)
        ctx.fill(prect)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.80, alpha: 1.0).cgColor)
        ctx.setLineWidth(2.0)
        ctx.stroke(prect)

        func mapY(_ v: Double) -> CGFloat {
            let t = (v - panel.yMin) / (panel.yMax - panel.yMin)
            return prect.minY + CGFloat(t) * prect.height
        }

        var yv = panel.yMin
        while yv <= panel.yMax + 0.01 {
            let py = mapY(yv)
            ctx.setStrokeColor(NSColor(calibratedWhite: abs(yv) < 0.01 ? 0.55 : 0.90, alpha: 1.0).cgColor)
            ctx.setLineWidth(abs(yv) < 0.01 ? 2.2 : 1.4)
            ctx.move(to: CGPoint(x: prect.minX, y: py))
            ctx.addLine(to: CGPoint(x: prect.maxX, y: py))
            ctx.strokePath()
            drawStr(String(format: "%.1f", yv) + panel.yUnit, x: prect.minX - 12, y: py - 12, size: 20, bold: abs(yv) < 0.01, color: colorFromHex("#334155"), align: "right")
            yv += panel.yStep
        }

        drawStr(panel.title, x: prect.minX, y: prect.maxY + 38, size: 26, bold: true, color: colorFromHex("#0F172A"))
        drawStr(panel.subtitle, x: prect.minX, y: prect.maxY + 8, size: 19, bold: false, color: colorFromHex("#475569"))

        let nCat = panel.categories.count
        let nSer = panel.series.count
        let catW = prect.width / CGFloat(nCat)
        let groupW = catW * 0.76
        let barW = groupW / CGFloat(nSer)
        let yZero = mapY(0.0)

        for cIdx in 0..<nCat {
            let catLeft = prect.minX + CGFloat(cIdx) * catW + (catW - groupW) / 2.0
            let catMid = prect.minX + (CGFloat(cIdx) + 0.5) * catW
            drawStr(panel.categories[cIdx], x: catMid, y: prect.minY - 42, size: 20, bold: true, color: colorFromHex("#1E293B"), align: "center")

            for sIdx in 0..<nSer {
                let val = panel.series[sIdx].values[cIdx]
                let bx = catLeft + CGFloat(sIdx) * barW + 3.0
                let bw = max(8.0, barW - 6.0)
                let py = mapY(val)
                let by = min(yZero, py)
                let bh = max(2.0, abs(py - yZero))

                let col = colorFromHex(panel.series[sIdx].colorHex)
                let bRect = CGRect(x: bx, y: by, width: bw, height: bh)
                let bPath = CGPath(roundedRect: bRect, cornerWidth: 5, cornerHeight: 5, transform: nil)
                ctx.setFillColor(col.cgColor)
                ctx.addPath(bPath)
                ctx.fillPath()

                let lblY = (val >= 0.0) ? (py + 6.0) : (py - 28.0)
                drawStr(String(format: "%.2f", val), x: bx + bw / 2.0, y: lblY, size: 16.5, bold: true, color: col, align: "center")
            }
        }
    }

    // Legend
    let ser0 = p.panels[0].series
    var lx: CGFloat = padL
    let ly: CGFloat = 86
    for s in ser0 {
        ctx.setFillColor(colorFromHex(s.colorHex).cgColor)
        ctx.fill(CGRect(x: lx, y: ly + 2, width: 28, height: 24))
        drawStr(s.name, x: lx + 38, y: ly, size: 21, bold: true, color: colorFromHex(s.colorHex))
        lx += 700
    }

    drawStr(p.footnote, x: padL, y: 32, size: 19, bold: false, color: colorFromHex("#475569"))
    drawStr(p.title, x: padL, y: CGFloat(h) - 76, size: 36, bold: true, color: colorFromHex("#0F172A"))
    drawStr(p.subtitle, x: padL, y: CGFloat(h) - 120, size: 21, bold: false, color: colorFromHex("#475569"))

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)

} else if mode == "withdrawal" {
    let p = try JSONDecoder().decode(WithdrawalPayload.self, from: rawData)
    let w = 2560
    let h = 1480
    NSGraphicsContext.saveGraphicsState()
    let (rep, ctx) = makeCanvas(width: w, height: h)

    let padL: CGFloat = 125
    let padR: CGFloat = 65
    let padB: CGFloat = 215
    let padT: CGFloat = 215
    let gap: CGFloat = 125
    let panelW = (CGFloat(w) - padL - padR - gap) / 2.0
    let panelH = CGFloat(h) - padT - padB

    // Left Panel: Grouped Bar Chart of 30Y Ruin Probability
    let bp = p.barPanel
    let rectL = CGRect(x: padL, y: padB, width: panelW, height: panelH)
    ctx.setFillColor(NSColor.white.cgColor)
    ctx.fill(rectL)
    ctx.setStrokeColor(NSColor(calibratedWhite: 0.80, alpha: 1.0).cgColor)
    ctx.setLineWidth(2.0)
    ctx.stroke(rectL)

    func mapYL(_ v: Double) -> CGFloat {
        let t = (v - bp.yMin) / (bp.yMax - bp.yMin)
        return rectL.minY + CGFloat(t) * rectL.height
    }
    var yv = bp.yMin
    while yv <= bp.yMax + 0.01 {
        let py = mapYL(yv)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.90, alpha: 1.0).cgColor)
        ctx.setLineWidth(1.4)
        ctx.move(to: CGPoint(x: rectL.minX, y: py))
        ctx.addLine(to: CGPoint(x: rectL.maxX, y: py))
        ctx.strokePath()
        drawStr(String(format: "%.0f%%", yv), x: rectL.minX - 10, y: py - 12, size: 19, bold: false, color: colorFromHex("#334155"), align: "right")
        yv += bp.yStep
    }
    drawStr(bp.title, x: rectL.minX, y: rectL.maxY + 38, size: 25, bold: true, color: colorFromHex("#0F172A"))
    drawStr(bp.subtitle, x: rectL.minX, y: rectL.maxY + 9, size: 18, bold: false, color: colorFromHex("#475569"))

    let nCat = bp.categories.count
    let nSer = bp.series.count
    let catW = rectL.width / CGFloat(nCat)
    let groupW = catW * 0.78
    let barW = groupW / CGFloat(nSer)
    for cIdx in 0..<nCat {
        let catLeft = rectL.minX + CGFloat(cIdx) * catW + (catW - groupW) / 2.0
        let catMid = rectL.minX + (CGFloat(cIdx) + 0.5) * catW
        drawStr(bp.categories[cIdx], x: catMid, y: rectL.minY - 40, size: 18.5, bold: true, color: colorFromHex("#1E293B"), align: "center")
        for sIdx in 0..<nSer {
            let val = bp.series[sIdx].values[cIdx]
            let bx = catLeft + CGFloat(sIdx) * barW + 2.0
            let bw = max(8.0, barW - 4.0)
            let py = mapYL(val)
            let bh = max(2.0, py - rectL.minY)
            let col = colorFromHex(bp.series[sIdx].colorHex)
            let bRect = CGRect(x: bx, y: rectL.minY, width: bw, height: bh)
            ctx.setFillColor(col.cgColor)
            ctx.addPath(CGPath(roundedRect: bRect, cornerWidth: 4, cornerHeight: 4, transform: nil))
            ctx.fillPath()
            drawStr(String(format: "%.1f%%", val), x: bx + bw / 2.0, y: py + 5.0, size: 15.0, bold: true, color: col, align: "center")
        }
    }

    // Left panel bar legend inside top-left of rectL
    var blx = rectL.minX + 24
    let bly = rectL.maxY - 44
    for s in bp.series {
        ctx.setFillColor(colorFromHex(s.colorHex).cgColor)
        ctx.fill(CGRect(x: blx, y: bly + 2, width: 22, height: 20))
        drawStr(s.name, x: blx + 30, y: bly, size: 18, bold: true, color: colorFromHex(s.colorHex))
        blx += 340
    }

    // Right Panel: 30Y Survival Curves under Early Crash
    let cp = p.curvePanel
    let rectR = CGRect(x: padL + panelW + gap, y: padB, width: panelW, height: panelH)
    ctx.setFillColor(NSColor.white.cgColor)
    ctx.fill(rectR)
    ctx.setStrokeColor(NSColor(calibratedWhite: 0.80, alpha: 1.0).cgColor)
    ctx.setLineWidth(2.0)
    ctx.stroke(rectR)

    let nPts = cp.lines[0].values.count
    func mapXR(_ idx: Int) -> CGFloat {
        return rectR.minX + (CGFloat(idx) / CGFloat(max(1, nPts - 1))) * rectR.width
    }
    func mapYR(_ v: Double) -> CGFloat {
        let t = (v - cp.yMin) / (cp.yMax - cp.yMin)
        return rectR.minY + CGFloat(t) * rectR.height
    }
    var yvR = cp.yMin
    while yvR <= cp.yMax + 0.01 {
        let py = mapYR(yvR)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.90, alpha: 1.0).cgColor)
        ctx.setLineWidth(1.4)
        ctx.move(to: CGPoint(x: rectR.minX, y: py))
        ctx.addLine(to: CGPoint(x: rectR.maxX, y: py))
        ctx.strokePath()
        drawStr(String(format: "%.0f%%", yvR), x: rectR.minX - 10, y: py - 12, size: 19, bold: false, color: colorFromHex("#334155"), align: "right")
        yvR += cp.yStep
    }
    for k in 0..<cp.xIndices.count {
        let xi = cp.xIndices[k]
        let px = mapXR(xi)
        ctx.setStrokeColor(NSColor(calibratedWhite: 0.92, alpha: 1.0).cgColor)
        ctx.setLineWidth(1.2)
        ctx.move(to: CGPoint(x: px, y: rectR.minY))
        ctx.addLine(to: CGPoint(x: px, y: rectR.maxY))
        ctx.strokePath()
        drawStr(cp.xLabels[k], x: px, y: rectR.minY - 40, size: 18.5, bold: false, color: colorFromHex("#334155"), align: "center")
    }
    drawStr(cp.title, x: rectR.minX, y: rectR.maxY + 38, size: 25, bold: true, color: colorFromHex("#0F172A"))
    drawStr(cp.subtitle, x: rectR.minX, y: rectR.maxY + 9, size: 18, bold: false, color: colorFromHex("#475569"))

    ctx.saveGState()
    ctx.clip(to: rectR)
    for ln in cp.lines {
        ctx.saveGState()
        ctx.setStrokeColor(colorFromHex(ln.colorHex).cgColor)
        ctx.setLineWidth(CGFloat(ln.lineWidth))
        if ln.dashed { ctx.setLineDash(phase: 0, lengths: [10, 6]) }
        ctx.move(to: CGPoint(x: mapXR(0), y: mapYR(ln.values[0])))
        for i in 1..<ln.values.count {
            ctx.addLine(to: CGPoint(x: mapXR(i), y: mapYR(ln.values[i])))
        }
        ctx.strokePath()
        ctx.restoreGState()
    }
    ctx.restoreGState()

    // Bottom legend for curves (2 rows of 3)
    var lx: CGFloat = padL
    var ly: CGFloat = 92
    for (idx, ln) in cp.lines.enumerated() {
        if idx == 3 {
            lx = padL
            ly = 48
        }
        ctx.saveGState()
        ctx.setStrokeColor(colorFromHex(ln.colorHex).cgColor)
        ctx.setLineWidth(4.5)
        if ln.dashed { ctx.setLineDash(phase: 0, lengths: [8, 5]) }
        ctx.move(to: CGPoint(x: lx, y: ly + 12))
        ctx.addLine(to: CGPoint(x: lx + 42, y: ly + 12))
        ctx.strokePath()
        ctx.restoreGState()
        drawStr(ln.label, x: lx + 52, y: ly, size: 19.5, bold: true, color: colorFromHex(ln.colorHex))
        lx += 780
    }

    drawStr(p.footnote, x: padL, y: 12, size: 17.5, bold: false, color: colorFromHex("#475569"))
    drawStr(p.title, x: padL, y: CGFloat(h) - 76, size: 36, bold: true, color: colorFromHex("#0F172A"))
    drawStr(p.subtitle, x: padL, y: CGFloat(h) - 120, size: 21, bold: false, color: colorFromHex("#475569"))

    NSGraphicsContext.restoreGraphicsState()
    try savePNG(rep, p.outPath)
}
'''


def render_robustness_charts(
    out_dir: Path,
    verdicts_data: dict[str, Any],
    gate3_data: dict[str, Any],
    gate5_data: dict[str, Any],
    gate8_data: dict[str, Any],
) -> dict[str, str]:
    """Compile Swift CoreGraphics binary once and render all 4 PNG charts."""
    out_heatmap = out_dir / "verdict_heatmap.png"
    out_intl = out_dir / "intl_crash_equity_curves.png"
    out_behav = out_dir / "behavior_xirr_loss_bars.png"
    out_withdraw = out_dir / "withdrawal_ruin_prob.png"

    # 1) Build Heatmap Payload
    col_headers = [
        "第 1 關\n資料體檢",
        "第 2 關\n樣本外測試",
        "第 3 關\n各國崩盤",
        "第 4 關\n稅金與匯率",
        "第 5 關\n真人行為",
        "第 6 關\n統計檢定",
        "第 7 關\n參數敏感度",
        "第 8 關\n人生階段",
        "最終總判定\nOverall",
    ]
    rec_labels = {
        "R1": "R1 一般人 (-50%)\n50% 0050 + 50% SPY",
        "R2": "R2 保守族 (-30%)\n60% 0050 + 40% 美短債",
        "R3": "R3 積極族 (-60%)\nM3b 1.4x / M3a 1.3x+濾網",
        "R4": "R4 槓桿戒律\n2 倍以上槓桿不建議",
        "R5": "R5 熊市回本結論\n0050 跌逾20%約 20 個月回前高",
    }
    gate_keys = [f"gate_{i}" for i in range(1, 9)]
    heat_rows = []
    for r_id in ["R1", "R2", "R3", "R4", "R5"]:
        rv = verdicts_data["by_recommendation"][r_id]
        cells = []
        for gk in gate_keys:
            g_entry = rv["gates"][gk]
            cells.append({
                "status": g_entry["verdict"],
                "detail": g_entry["short_metric"],
            })
        cells.append({
            "status": rv["overall_verdict"],
            "detail": rv["overall_short_note"],
        })
        heat_rows.append({
            "recId": r_id,
            "recLabel": rec_labels[r_id],
            "cells": cells,
        })

    heatmap_payload = {
        "outPath": str(out_heatmap),
        "title": "八關穩健性驗證總表：目前五項投資推薦（R1–R5）到底站不站得住？",
        "subtitle": "嚴格套用預先提交（Commit 1）之判定標準：全過＝站得住｜任一有條件且無不過＝有條件成立｜任一不過＝被推翻",
        "colHeaders": col_headers,
        "rows": heat_rows,
    }

    # 2) Build International Crash Equity Curves Payload (3 panels)
    line_styles = [
        ("INV", "累計投入本金（1萬/月）", "#64748B", True, 2.6),
        ("R1", "R1 一般人：50% 0050 + 50% SPY", "#0D9488", False, 4.2),
        ("R2", "R2 保守：60% 0050 + 40% 美短債", "#2563EB", False, 3.8),
        ("R3_M3b", "R3 (M3b)：40/40/20 年平衡 (1.4x)", "#EA580C", False, 3.6),
        ("R3_M3a", "R3 (M3a)：70/30 + 200MA濾網 (1.3x)", "#7C3AED", False, 3.2),
        ("R4_M4", "R4 對照：100% 2倍槓桿 (2.0x)", "#DC2626", False, 3.2),
    ]
    spot = gate3_data["featured_curves"]
    panels_intl = []
    for sp_key, p_title, p_sub in [
        ("JP_1989_20Y_both", "日本 1989-12 泡沫頂點進場 20 年（兩市場皆走日經+1.5%息）", "失落二十年極限壓力：R2 靠短債全身而退，2倍槓桿 R4 慘賠逾半"),
        ("CN_2007_10Y_both", "上證 2007-10 泡沫頂點進場 10 年（兩市場皆走上證+2.0%息）", "6,124 點崩跌後劇烈震盪：定投攤平使 R1/R2 獲利，高槓桿大幅落後"),
        ("JP_1989_10Y_tw_only", "日本 1989-12 頂點進場 10 年（僅台股走日本、美股維持 SPY）", "跨國分散威力：就算台灣失落十年，R1 靠一半美股照樣翻倍賺！"),
    ]:
        sc_obj = spot[sp_key]
        dates_m = sc_obj["dates"]
        n_pts = len(dates_m)
        x_idxs = [0, n_pts // 4, n_pts // 2, (3 * n_pts) // 4, n_pts - 1]
        x_lbls = [dates_m[i][:7] for i in x_idxs]
        lines = []
        y_max = 0.0
        for k_id, lbl, hx, dsh, lw in line_styles:
            vals = sc_obj["curves_wan"][k_id]
            y_max = max(y_max, max(vals))
            lines.append({
                "label": lbl,
                "values": vals,
                "colorHex": hx,
                "dashed": dsh,
                "lineWidth": lw,
            })
        step = 100.0 if y_max > 360.0 else 50.0
        y_ceil = math.ceil((y_max + 20.0) / step) * step
        panels_intl.append({
            "title": p_title,
            "subtitle": p_sub,
            "xLabels": x_lbls,
            "xIndices": x_idxs,
            "yMin": 0.0,
            "yMax": y_ceil,
            "yStep": step,
            "yUnit": "萬",
            "lines": lines,
        })

    intl_payload = {
        "outPath": str(out_intl),
        "title": "第 3 關跨國崩盤壓力測試：如果台股或美股走上「日本失落二十年」或「上證十年不回頭」？",
        "subtitle": "每月固定扣款 1 萬台幣（10 年本金 120 萬、20 年本金 240 萬）｜指數皆加計保守股息假設（日本 1.5%、上證 2.0%）",
        "panels": panels_intl,
    }

    # 3) Build Behavior XIRR Loss Grouped Bar Payload
    beh_summary = gate5_data["summary_by_strategy"]
    strat_order = [
        ("R2", "R2 保守\n(60/40股債)"),
        ("R1", "R1 一般\n(50/50台美)"),
        ("M2a", "純台股對照\n(100% 0050)"),
        ("R3_M3a", "R3a 積極\n(1.3x+濾網)"),
        ("R3_M3b", "R3b 積極\n(1.4x年平衡)"),
    ]
    cats_beh = [lbl.replace("\n", " ") for _, lbl in strat_order]
    s_a_vals = [abs(float(beh_summary[sid]["A_pause_6m"]["mean_xirr_loss_pp"])) for sid, _ in strat_order]
    s_b_vals = [abs(float(beh_summary[sid]["B_panic_sell_20pct"]["mean_xirr_loss_pp"])) for sid, _ in strat_order]
    s_c_vals = [abs(float(beh_summary[sid]["C_unemployed_12m_withdraw"]["mean_xirr_loss_pp"])) for sid, _ in strat_order]
    max_loss = max(max(s_a_vals), max(s_b_vals), max(s_c_vals), 2.5)
    y_ceil_b = math.ceil((max_loss + 0.4) / 0.5) * 0.5

    behav_payload = {
        "outPath": str(out_behav),
        "title": "第 5 關真人行為壓力測試：恐慌停扣、殺低賣出、股災失業提領，平均少賺多少年化報酬（XIRR）？",
        "subtitle": "橫跨全歷史 213 個滾動 10 年梯次實測（數值越低代表越「抗人性」，行為失誤造成的報酬折損越小）",
        "footnote": "註：情境 A/B 以投資人自身帳戶淨值自高點回撤逾 30% / 40% 觸發；情境 C 失業屬總體經濟衝擊，統一以大盤跌逾 30% 觸發停扣 12 個月並提領 18 萬生活費。",
        "panels": [
            {
                "title": "三種常見人性與人生意外造成的平均年化報酬損失（百分點，越低越抗人性）",
                "subtitle": "R2（60/40 股債）因帳面極少跌破 -30% 且從未跌破 -40%，幾乎免疫恐慌殺低；高波動 R3 行為代價最高",
                "categories": cats_beh,
                "yMin": 0.0,
                "yMax": y_ceil_b,
                "yStep": 0.5 if y_ceil_b <= 4.5 else 1.0,
                "yUnit": "%",
                "series": [
                    {"name": "情境 A：跌逾 30% 停扣 6 個月", "colorHex": "#2563EB", "values": s_a_vals},
                    {"name": "情境 B：跌逾 40% 恐慌賣 20%（回前高買回）", "colorHex": "#D97706", "values": s_b_vals},
                    {"name": "情境 C：跌逾 30% 失業停扣 12 月＋領 6 個月生活費", "colorHex": "#DC2626", "values": s_c_vals},
                ],
            }
        ],
    }

    # 4) Build Retirement Withdrawal Ruin Probability & Survival Curve Payload
    wd_ports = [
        ("P_30_70", "30/70 股債", "#0F766E", True, 3.4),
        ("P_40_60", "40/60 股債", "#16A34A", False, 3.8),
        ("R2", "R2 (60/40)", "#2563EB", False, 4.2),
        ("R1", "R1 (50/50純股)", "#0D9488", False, 3.8),
        ("R3_M3a", "R3a (1.3x+濾網)", "#7C3AED", False, 3.4),
        ("R3_M3b", "R3b (1.4x年平衡)", "#EA580C", False, 3.6),
    ]
    wd_res = gate8_data["withdrawal_30y"]["portfolios"]
    cats_wd = [lbl for _, lbl, _, _, _ in wd_ports]
    all_ruin_vals = [float(wd_res[pid]["all_paths"]["ruin_prob_pct"]) for pid, _, _, _, _ in wd_ports]
    crash_ruin_vals = [float(wd_res[pid]["early_crash_paths"]["ruin_prob_pct"]) for pid, _, _, _, _ in wd_ports]
    max_ruin = max(max(all_ruin_vals), max(crash_ruin_vals), 25.0)
    y_ceil_w = min(100.0, math.ceil((max_ruin + 5.0) / 10.0) * 10.0)

    surv_lines = []
    for pid, lbl, hx, dsh, lw in wd_ports:
        surv_lines.append({
            "label": f"{lbl}（遇早年崩盤存活率）",
            "values": wd_res[pid]["early_crash_paths"]["survival_by_year_pct"],
            "colorHex": hx,
            "dashed": dsh,
            "lineWidth": lw,
        })

    withdraw_payload = {
        "outPath": str(out_withdraw),
        "title": "第 8 關退休提領期實測：60 歲抱 1,000 萬台幣退休、每年領 4%（隨 2% 通膨調升），撐得過 30 年嗎？",
        "subtitle": "比較累積期推薦（R1–R3）與防禦型股債配置（40/60、30/70）：剛退休前 3 年就遇大跌（序列風險）是最大殺手！",
        "footnote": "註：第一年提領 40 萬台幣（每月領 3.33 萬），每年依 2% 通膨調高提領額並執行年度再平衡；模擬 30 年（60 歲至 90 歲）。",
        "barPanel": {
            "title": "30 年內把錢花光的破產機率（越低越安全）",
            "subtitle": "對比「全體退休梯次」vs「退休前 3 年就撞上股災」",
            "categories": cats_wd,
            "yMin": 0.0,
            "yMax": y_ceil_w,
            "yStep": 10.0 if y_ceil_w > 35.0 else 5.0,
            "yUnit": "%",
            "series": [
                {"name": "全體路徑 30 年耗盡機率", "colorHex": "#2563EB", "values": all_ruin_vals},
                {"name": "剛退休就遇大跌（序列風險）耗盡機率", "colorHex": "#DC2626", "values": crash_ruin_vals},
            ],
        },
        "curvePanel": {
            "title": "剛退休就遇股災：資產未耗盡之「年度存活率曲線」",
            "subtitle": "高波動槓桿 R3 在退休初期賣股失血過重，40/60 與 R2 穩健護航至 90 歲",
            "xLabels": ["60歲(起)", "66歲", "72歲", "78歲", "84歲", "90歲(30年)"],
            "xIndices": [0, 6, 12, 18, 24, 30],
            "yMin": max(0.0, math.floor((min(min(ln["values"]) for ln in surv_lines) - 8.0) / 10.0) * 10.0),
            "yMax": 100.0,
            "yStep": 10.0,
            "yUnit": "%",
            "lines": surv_lines,
        },
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        swift_src = tmp_p / "render_robustness.swift"
        swift_bin = tmp_p / "render_robustness_bin"
        swift_src.write_text(SWIFT_RENDERER_SOURCE, encoding="utf-8")
        subprocess.run(["/usr/bin/swiftc", "-O", str(swift_src), "-o", str(swift_bin)], check=True)

        for mode_name, payload in [
            ("heatmap", heatmap_payload),
            ("multicurve", intl_payload),
            ("groupedbars", behav_payload),
            ("withdrawal", withdraw_payload),
        ]:
            j_path = tmp_p / f"{mode_name}.json"
            j_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            subprocess.run([str(swift_bin), mode_name, str(j_path)], check=True)

    return {
        "verdict_heatmap": out_heatmap.name,
        "intl_crash_equity_curves": out_intl.name,
        "behavior_xirr_loss_bars": out_behav.name,
        "withdrawal_ruin_prob": out_withdraw.name,
    }
