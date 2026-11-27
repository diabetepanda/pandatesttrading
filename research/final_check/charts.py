#!/usr/bin/env python3
"""High-DPI Traditional Chinese PNG chart renderer for research/final_check/ via macOS Swift CoreGraphics (PingFang TC).

Generates 8 dedicated PNG charts (1 per test):
  1. test1_aggressive_filter_rules.png
  2. test2_estate_tax_domicile.png
  3. test3_real_bonds_2022_retire.png
  4. test4_lifetime_continuous_fan.png
  5. test5_withdrawal_protection.png
  6. test6_shiller_crisis_paths.png
  7. test7_fx_swings_impact.png
  8. test8_different_people_ages.png
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
RESULTS_JSON_PATH = BASE_DIR / "results.json"

SWIFT_CHART_SOURCE = r'''
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
    ctx.setFillColor(NSColor(calibratedRed: 0.985, green: 0.989, blue: 0.996, alpha: 1.0).cgColor)
    ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
    return (rep, ctx)
}

func colorFromHex(_ hex: String) -> NSColor {
    var s = hex.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.hasPrefix("#") { s.removeFirst() }
    var rgb: UInt64 = 0
    Scanner(string: s).scanHexInt64(&rgb)
    return NSColor(
        calibratedRed: CGFloat((rgb >> 16) & 0xFF) / 255.0,
        green: CGFloat((rgb >> 8) & 0xFF) / 255.0,
        blue: CGFloat(rgb & 0xFF) / 255.0,
        alpha: 1.0
    )
}

func drawStr(_ text: String, x: CGFloat, y: CGFloat, size: CGFloat, bold: Bool = false, color: NSColor = colorFromHex("#0F172A"), align: String = "left") {
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

func savePNG(_ rep: NSBitmapImageRep, _ path: String) throws {
    let data = rep.representation(using: .png, properties: [:])!
    try data.write(to: URL(fileURLWithPath: path))
}

struct BarSeries: Codable {
    let label: String
    let colorHex: String
    let values: [Double]
    let valueLabels: [String]
}

struct BarPanel: Codable {
    let panelTitle: String
    let yUnit: String
    let categories: [String]
    let series: [BarSeries]
    let yMin: Double
    let yMax: Double
    let refLine: Double?
    let refLabel: String?
}

struct BarChartPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let footnote: String
    let panels: [BarPanel]
}

struct LineSeries: Codable {
    let label: String
    let colorHex: String
    let dashed: Bool
    let values: [Double]
}

struct LinePanel: Codable {
    let panelTitle: String
    let xLabel: String
    let yLabel: String
    let xValues: [Double]
    let series: [LineSeries]
    let yMin: Double
    let yMax: Double
    let vLines: [Double]?
    let vLabels: [String]?
    let hLine: Double?
    let hLabel: String?
}

struct LineChartPayload: Codable {
    let outPath: String
    let title: String
    let subtitle: String
    let footnote: String
    let panels: [LinePanel]
}

struct AllChartsPayload: Codable {
    let barCharts: [BarChartPayload]
    let lineCharts: [LineChartPayload]
}

func renderBarChart(_ p: BarChartPayload) throws {
    let W = 1520
    let H = 820
    let (rep, ctx) = makeCanvas(width: W, height: H)

    drawStr(p.title, x: 48, y: CGFloat(H - 58), size: 28, bold: true, color: colorFromHex("#0F172A"))
    drawStr(p.subtitle, x: 48, y: CGFloat(H - 92), size: 15, bold: false, color: colorFromHex("#475569"))

    let nPanels = p.panels.count
    let marginL: CGFloat = 48
    let marginR: CGFloat = 48
    let gap: CGFloat = 46
    let panelW = (CGFloat(W) - marginL - marginR - gap * CGFloat(max(0, nPanels - 1))) / CGFloat(max(1, nPanels))
    let plotBottom: CGFloat = 135
    let plotTop: CGFloat = CGFloat(H - 185)
    let plotH = plotTop - plotBottom

    for (pi, panel) in p.panels.enumerated() {
        let px0 = marginL + CGFloat(pi) * (panelW + gap)
        let chartX0 = px0 + 68
        let chartW = panelW - 86

        // Panel card background
        ctx.setFillColor(NSColor.white.cgColor)
        ctx.setStrokeColor(colorFromHex("#E2E8F0").cgColor)
        ctx.setLineWidth(1.2)
        let cardRect = CGRect(x: px0, y: 68, width: panelW, height: CGFloat(H - 190))
        ctx.fill(cardRect)
        ctx.stroke(cardRect)

        drawStr(panel.panelTitle, x: px0 + 20, y: plotTop + 34, size: 18, bold: true, color: colorFromHex("#1E293B"))

        // Legend inside top-right of panel
        var legX = px0 + panelW - 18
        for s in panel.series.reversed() {
            let font = NSFont(name: "PingFangTC-Regular", size: 13) ?? NSFont.systemFont(ofSize: 13)
            let tw = NSString(string: s.label).size(withAttributes: [.font: font]).width
            legX -= tw
            drawStr(s.label, x: legX, y: plotTop + 10, size: 13, bold: false, color: colorFromHex("#334155"))
            legX -= 18
            ctx.setFillColor(colorFromHex(s.colorHex).cgColor)
            ctx.fill(CGRect(x: legX, y: plotTop + 13, width: 12, height: 12))
            legX -= 14
        }

        // Y-axis grid
        let ySpan = max(panel.yMax - panel.yMin, 1e-6)
        func yToPy(_ v: Double) -> CGFloat {
            return plotBottom + CGFloat((v - panel.yMin) / ySpan) * plotH
        }
        let nTicks = 5
        for ti in 0...nTicks {
            let tv = panel.yMin + ySpan * Double(ti) / Double(nTicks)
            let py = yToPy(tv)
            ctx.setStrokeColor(colorFromHex("#E2E8F0").cgColor)
            ctx.setLineWidth(1.0)
            ctx.move(to: CGPoint(x: chartX0, y: py))
            ctx.addLine(to: CGPoint(x: chartX0 + chartW, y: py))
            ctx.strokePath()
            let lbl = abs(tv) >= 100 ? String(format: "%.0f", tv) : String(format: "%.1f", tv)
            drawStr(lbl + panel.yUnit, x: chartX0 - 8, y: py - 7, size: 12, bold: false, color: colorFromHex("#64748B"), align: "right")
        }

        // Zero baseline if within range
        let zeroY = yToPy(min(max(0.0, panel.yMin), panel.yMax))
        ctx.setStrokeColor(colorFromHex("#94A3B8").cgColor)
        ctx.setLineWidth(1.5)
        ctx.move(to: CGPoint(x: chartX0, y: zeroY))
        ctx.addLine(to: CGPoint(x: chartX0 + chartW, y: zeroY))
        ctx.strokePath()

        // Optional reference line
        if let rv = panel.refLine {
            let ry = yToPy(rv)
            ctx.saveGState()
            ctx.setStrokeColor(colorFromHex("#DC2626").cgColor)
            ctx.setLineWidth(1.8)
            ctx.setLineDash(phase: 0, lengths: [6, 4])
            ctx.move(to: CGPoint(x: chartX0, y: ry))
            ctx.addLine(to: CGPoint(x: chartX0 + chartW, y: ry))
            ctx.strokePath()
            ctx.restoreGState()
            if let rl = panel.refLabel {
                let isLeft = rv < 0.0
                let rx = isLeft ? (chartX0 + 6) : (chartX0 + chartW - 4)
                let rAlign = isLeft ? "left" : "right"
                drawStr(rl, x: rx, y: ry + 4, size: 12, bold: true, color: colorFromHex("#DC2626"), align: rAlign)
            }
        }

        // Bars
        let nCat = max(1, panel.categories.count)
        let nSer = max(1, panel.series.count)
        let groupW = chartW / CGFloat(nCat)
        let usableW = groupW * 0.74
        let barW = usableW / CGFloat(nSer)

        for ci in 0..<nCat {
            let gx0 = chartX0 + CGFloat(ci) * groupW + (groupW - usableW) / 2.0
            for (si, ser) in panel.series.enumerated() {
                if ci >= ser.values.count { continue }
                let v = ser.values[ci]
                let bx = gx0 + CGFloat(si) * barW + 2
                let bw = max(6, barW - 4)
                let vy = yToPy(v)
                let rectY = min(zeroY, vy)
                let rectH = max(2, abs(vy - zeroY))
                ctx.setFillColor(colorFromHex(ser.colorHex).cgColor)
                ctx.fill(CGRect(x: bx, y: rectY, width: bw, height: rectH))

                let vlbl = ci < ser.valueLabels.count ? ser.valueLabels[ci] : ""
                let ly = v >= 0 ? (vy + 5) : (vy - 18)
                drawStr(vlbl, x: bx + bw / 2.0, y: ly, size: 11, bold: true, color: colorFromHex("#1E293B"), align: "center")
            }
            // Category label (support newline split)
            let catLines = panel.categories[ci].components(separatedBy: "\n")
            let cx = chartX0 + (CGFloat(ci) + 0.5) * groupW
            for (li, lineTxt) in catLines.enumerated() {
                drawStr(lineTxt, x: cx, y: plotBottom - 24 - CGFloat(li) * 16, size: 12, bold: false, color: colorFromHex("#334155"), align: "center")
            }
        }
    }

    drawStr(p.footnote, x: 48, y: 24, size: 12, bold: false, color: colorFromHex("#64748B"))
    try savePNG(rep, p.outPath)
}

func renderLineChart(_ p: LineChartPayload) throws {
    let W = 1520
    let H = 820
    let (rep, ctx) = makeCanvas(width: W, height: H)

    drawStr(p.title, x: 48, y: CGFloat(H - 58), size: 28, bold: true, color: colorFromHex("#0F172A"))
    drawStr(p.subtitle, x: 48, y: CGFloat(H - 92), size: 15, bold: false, color: colorFromHex("#475569"))

    let nPanels = p.panels.count
    let marginL: CGFloat = 48
    let marginR: CGFloat = 48
    let gap: CGFloat = 42
    let panelW = (CGFloat(W) - marginL - marginR - gap * CGFloat(max(0, nPanels - 1))) / CGFloat(max(1, nPanels))
    let plotBottom: CGFloat = 135
    let plotTop: CGFloat = CGFloat(H - 210)
    let plotH = plotTop - plotBottom

    for (pi, panel) in p.panels.enumerated() {
        let px0 = marginL + CGFloat(pi) * (panelW + gap)
        let chartX0 = px0 + 78
        let chartW = panelW - 102

        ctx.setFillColor(NSColor.white.cgColor)
        ctx.setStrokeColor(colorFromHex("#E2E8F0").cgColor)
        ctx.setLineWidth(1.2)
        let cardRect = CGRect(x: px0, y: 68, width: panelW, height: CGFloat(H - 190))
        ctx.fill(cardRect)
        ctx.stroke(cardRect)

        drawStr(panel.panelTitle, x: px0 + 20, y: plotTop + 54, size: 18, bold: true, color: colorFromHex("#1E293B"))

        // Legend row (well above Y-axis label)
        var legX = chartX0
        for s in panel.series {
            ctx.saveGState()
            ctx.setStrokeColor(colorFromHex(s.colorHex).cgColor)
            ctx.setLineWidth(3.0)
            if s.dashed { ctx.setLineDash(phase: 0, lengths: [6, 4]) }
            ctx.move(to: CGPoint(x: legX, y: plotTop + 34))
            ctx.addLine(to: CGPoint(x: legX + 20, y: plotTop + 34))
            ctx.strokePath()
            ctx.restoreGState()
            drawStr(s.label, x: legX + 25, y: plotTop + 26, size: 12.5, bold: false, color: colorFromHex("#334155"))
            let font = NSFont(name: "PingFangTC-Regular", size: 12.5) ?? NSFont.systemFont(ofSize: 12.5)
            let tw = NSString(string: s.label).size(withAttributes: [.font: font]).width
            legX += 42 + tw
        }

        let xMin = panel.xValues.first ?? 0.0
        let xMax = panel.xValues.last ?? 1.0
        let xSpan = max(xMax - xMin, 1e-6)
        let ySpan = max(panel.yMax - panel.yMin, 1e-6)

        func xToPx(_ xv: Double) -> CGFloat {
            return chartX0 + CGFloat((xv - xMin) / xSpan) * chartW
        }
        func yToPy(_ yv: Double) -> CGFloat {
            let cl = min(max(yv, panel.yMin), panel.yMax)
            return plotBottom + CGFloat((cl - panel.yMin) / ySpan) * plotH
        }

        // Y grid
        for ti in 0...5 {
            let tv = panel.yMin + ySpan * Double(ti) / 5.0
            let py = yToPy(tv)
            ctx.setStrokeColor(colorFromHex("#E2E8F0").cgColor)
            ctx.setLineWidth(1.0)
            ctx.move(to: CGPoint(x: chartX0, y: py))
            ctx.addLine(to: CGPoint(x: chartX0 + chartW, y: py))
            ctx.strokePath()
            let lbl = String(format: "%.0f", tv)
            drawStr(lbl, x: chartX0 - 8, y: py - 7, size: 12, bold: false, color: colorFromHex("#64748B"), align: "right")
        }
        drawStr(panel.yLabel, x: chartX0 - 8, y: plotTop + 6, size: 11.5, bold: true, color: colorFromHex("#475569"), align: "right")

        // X grid
        let xStep = xSpan > 40 ? 10.0 : 5.0
        var xv = ceil(xMin / xStep) * xStep
        while xv <= xMax + 0.01 {
            let px = xToPx(xv)
            ctx.setStrokeColor(colorFromHex("#F1F5F9").cgColor)
            ctx.setLineWidth(1.0)
            ctx.move(to: CGPoint(x: px, y: plotBottom))
            ctx.addLine(to: CGPoint(x: px, y: plotTop))
            ctx.strokePath()
            drawStr(String(format: "%.0f", xv), x: px, y: plotBottom - 22, size: 12, bold: false, color: colorFromHex("#475569"), align: "center")
            xv += xStep
        }
        drawStr(panel.xLabel, x: chartX0 + chartW / 2.0, y: plotBottom - 46, size: 13, bold: true, color: colorFromHex("#334155"), align: "center")

        // Optional vertical marker lines
        if let vls = panel.vLines {
            for (vi, vl) in vls.enumerated() {
                let vx = xToPx(vl)
                ctx.saveGState()
                ctx.setStrokeColor(colorFromHex("#64748B").cgColor)
                ctx.setLineWidth(1.4)
                ctx.setLineDash(phase: 0, lengths: [5, 4])
                ctx.move(to: CGPoint(x: vx, y: plotBottom))
                ctx.addLine(to: CGPoint(x: vx, y: plotTop))
                ctx.strokePath()
                ctx.restoreGState()
                if let vlbls = panel.vLabels, vi < vlbls.count {
                    drawStr(vlbls[vi], x: vx + 4, y: plotTop - 18, size: 11.5, bold: true, color: colorFromHex("#475569"))
                }
            }
        }

        // Optional horizontal marker line
        if let hl = panel.hLine {
            let hy = yToPy(hl)
            ctx.saveGState()
            ctx.setStrokeColor(colorFromHex("#DC2626").cgColor)
            ctx.setLineWidth(1.5)
            ctx.setLineDash(phase: 0, lengths: [6, 4])
            ctx.move(to: CGPoint(x: chartX0, y: hy))
            ctx.addLine(to: CGPoint(x: chartX0 + chartW, y: hy))
            ctx.strokePath()
            ctx.restoreGState()
            if let hlbl = panel.hLabel {
                drawStr(hlbl, x: chartX0 + chartW - 4, y: hy + 4, size: 11.5, bold: true, color: colorFromHex("#DC2626"), align: "right")
            }
        }

        // Draw series lines
        for ser in panel.series {
            if ser.values.count < 2 { continue }
            ctx.saveGState()
            ctx.setStrokeColor(colorFromHex(ser.colorHex).cgColor)
            ctx.setLineWidth(2.8)
            if ser.dashed { ctx.setLineDash(phase: 0, lengths: [7, 4]) }
            let nPts = min(panel.xValues.count, ser.values.count)
            ctx.move(to: CGPoint(x: xToPx(panel.xValues[0]), y: yToPy(ser.values[0])))
            for i in 1..<nPts {
                ctx.addLine(to: CGPoint(x: xToPx(panel.xValues[i]), y: yToPy(ser.values[i])))
            }
            ctx.strokePath()
            ctx.restoreGState()
        }
    }

    drawStr(p.footnote, x: 48, y: 24, size: 12, bold: false, color: colorFromHex("#64748B"))
    try savePNG(rep, p.outPath)
}

let args = CommandLine.arguments
guard args.count >= 2 else { exit(1) }
let data = try Data(contentsOf: URL(fileURLWithPath: args[1]))
let payload = try JSONDecoder().decode(AllChartsPayload.self, from: data)
for bc in payload.barCharts { try renderBarChart(bc) }
for lc in payload.lineCharts { try renderLineChart(lc) }
print("Rendered \(payload.barCharts.count) bar charts and \(payload.lineCharts.count) line charts.")
'''


def build_chart_payloads(res: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    bar_charts: list[dict[str, Any]] = []
    line_charts: list[dict[str, Any]] = []

    # 1) Test 1: Aggressive Filter Rules A/B/C + Age-50 Exit
    t1 = res["test_1_aggressive_filter"]
    tw = t1["tw_only_0band"]
    bt = t1["bootstrap_tw_only"]
    a50 = t1["age50_exit_comparison"]
    bar_charts.append({
        "outPath": str(out_dir / "test1_aggressive_filter_rules.png"),
        "title": "測試 1：積極型 200 日線濾網規則 A / B / C 實測與 50 歲出清對比",
        "subtitle": "台股積極部位（0050 80% + 00631L 20%，含 0.1425% 手續費與 0.1% ETF 證交稅）；右圖為 30→60 歲 00631L 出清時機比較",
        "footnote": "資料區間：1997-07～2026-11（00631L 2015-08 前以公式模擬）；Bootstrap 採 Stationary 平均 3 年區塊抽樣。",
        "panels": [
            {
                "panelTitle": "左圖：規則 A / B / C 報酬與破 −60% 機率",
                "yUnit": "%",
                "categories": [
                    f"規則 A\n(僅新扣款切換)\n{tw['rule_A']['full_history']['total_events_per_year']:.1f}次/年",
                    f"規則 B\n(全賣出再買回)\n{tw['rule_B']['full_history']['total_events_per_year']:.1f}次/年",
                    f"規則 C\n(無濾網年平衡)\n{tw['rule_C']['full_history']['total_events_per_year']:.1f}次/年",
                ],
                "series": [
                    {
                        "label": "滾動10年中位XIRR",
                        "colorHex": "#2563EB",
                        "values": [tw[f"rule_{r}"]["rolling_10y"]["median_xirr_pct"] for r in ("A", "B", "C")],
                        "valueLabels": [f"{tw[f'rule_{r}']['rolling_10y']['median_xirr_pct']:.2f}%" for r in ("A", "B", "C")],
                    },
                    {
                        "label": "Bootstrap破−60%機率",
                        "colorHex": "#DC2626",
                        "values": [bt[f"rule_{r}"]["prob_mdd_lt_60pct"] for r in ("A", "B", "C")],
                        "valueLabels": [f"{bt[f'rule_{r}']['prob_mdd_lt_60pct']:.1f}%" for r in ("A", "B", "C")],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(15.0, max(tw[f"rule_{r}"]["rolling_10y"]["median_xirr_pct"] for r in ("A", "B", "C")) + 4.0, max(bt[f"rule_{r}"]["prob_mdd_lt_60pct"] for r in ("A", "B", "C")) + 4.0),
                "refLine": 10.0,
                "refLabel": "破 −60% 機率上限 10%",
            },
            {
                "panelTitle": "右圖：50 歲賣掉 00631L vs 55 歲賣 vs 不賣（60 歲資產，萬元）",
                "yUnit": "萬",
                "categories": ["50 歲 1 月\n出清 00631L", "55 歲滑降時\n同步出清", "一路不賣\n持有至 60 歲"],
                "series": [
                    {
                        "label": "60歲 P10 資產(差運氣)",
                        "colorHex": "#0D9488",
                        "values": [a50[k]["age60_p10_twd"] / 10000.0 for k in ("sell_at_50", "sell_at_55_glidepath", "never_sell_until_60")],
                        "valueLabels": [f"{a50[k]['age60_p10_twd']/10000:.0f}萬" for k in ("sell_at_50", "sell_at_55_glidepath", "never_sell_until_60")],
                    },
                    {
                        "label": "60歲 P50 中位數資產",
                        "colorHex": "#7C3AED",
                        "values": [a50[k]["age60_p50_twd"] / 10000.0 for k in ("sell_at_50", "sell_at_55_glidepath", "never_sell_until_60")],
                        "valueLabels": [f"{a50[k]['age60_p50_twd']/10000:.0f}萬" for k in ("sell_at_50", "sell_at_55_glidepath", "never_sell_until_60")],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(a50[k]["age60_p50_twd"] / 10000.0 for k in a50) * 1.25,
                "refLine": None,
                "refLabel": None,
            },
        ],
    })

    # 2) Test 2: US Estate Tax & ETF Domicile (60/75/90 death ages)
    t2 = res["test_2_estate_tax_domicile"]
    es10 = t2["estate_tax_by_death_age"]["dca_10k_per_month"]
    es30 = t2["estate_tax_by_death_age"]["dca_30k_per_month"]
    bar_charts.append({
        "outPath": str(out_dir / "test2_estate_tax_domicile.png"),
        "title": "測試 2：美國註冊 ETF（SPY/VOO）vs 愛爾蘭註冊（CSPX）遺產稅實拿差距",
        "subtitle": "非美國居民美國境內資產逾 USD 60,000 適用 18%–40% 累進遺產稅（匯率 32 元）；比較 60 / 75 / 90 歲過世留給家人金額",
        "footnote": "左圖為月投 1 萬（30→60歲中位資產）；右圖為月投 3 萬。深藍為僅美股課稅，紅柱為美股+美註冊債ETF同為美國資產課稅。",
        "panels": [
            {
                "panelTitle": "左圖：月投 1 萬情境 — 過世後美國遺產稅損失（萬元）",
                "yUnit": "萬",
                "categories": ["60 歲過世", "75 歲過世", "90 歲過世"],
                "series": [
                    {
                        "label": "僅美股30%買SPY/VOO稅金",
                        "colorHex": "#2563EB",
                        "values": [es10[a]["us_equity_only"]["spy_voo_estate_tax_twd"] / 10000.0 for a in ("60", "75", "90")],
                        "valueLabels": [f"{es10[a]['us_equity_only']['spy_voo_estate_tax_twd']/10000:.0f}萬" for a in ("60", "75", "90")],
                    },
                    {
                        "label": "美股30%+美債40%皆美註冊稅金",
                        "colorHex": "#DC2626",
                        "values": [es10[a]["us_equity_plus_us_bond_etf"]["us_domiciled_estate_tax_twd"] / 10000.0 for a in ("60", "75", "90")],
                        "valueLabels": [f"{es10[a]['us_equity_plus_us_bond_etf']['us_domiciled_estate_tax_twd']/10000:.0f}萬" for a in ("60", "75", "90")],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(es10[a]["us_equity_plus_us_bond_etf"]["us_domiciled_estate_tax_twd"] / 10000.0 for a in ("60", "75", "90")) * 1.25,
                "refLine": 100.0,
                "refLabel": "判斷門檻：100 萬台幣",
            },
            {
                "panelTitle": "右圖：月投 3 萬情境 — 過世後美國遺產稅損失（萬元）",
                "yUnit": "萬",
                "categories": ["60 歲過世", "75 歲過世", "90 歲過世"],
                "series": [
                    {
                        "label": "僅美股30%買SPY/VOO稅金",
                        "colorHex": "#2563EB",
                        "values": [es30[a]["us_equity_only"]["spy_voo_estate_tax_twd"] / 10000.0 for a in ("60", "75", "90")],
                        "valueLabels": [f"{es30[a]['us_equity_only']['spy_voo_estate_tax_twd']/10000:.0f}萬" for a in ("60", "75", "90")],
                    },
                    {
                        "label": "美股30%+美債40%皆美註冊稅金",
                        "colorHex": "#DC2626",
                        "values": [es30[a]["us_equity_plus_us_bond_etf"]["us_domiciled_estate_tax_twd"] / 10000.0 for a in ("60", "75", "90")],
                        "valueLabels": [f"{es30[a]['us_equity_plus_us_bond_etf']['us_domiciled_estate_tax_twd']/10000:.0f}萬" for a in ("60", "75", "90")],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(es30[a]["us_equity_plus_us_bond_etf"]["us_domiciled_estate_tax_twd"] / 10000.0 for a in ("60", "75", "90")) * 1.25,
                "refLine": None,
                "refLabel": None,
            },
        ],
    })

    # 3) Test 3: Real Bonds 2022 Selloff & 30Y Retirement Ruin
    t3 = res["test_3_real_bonds"]
    s22 = t3["selloff_2022_and_history"]
    r30 = t3["retirement_30y_comparison"]
    b_keys = ["SHY", "VGIT", "IEF", "IRX"]
    b_cats = ["SHY\n(1-3年短債)", "VGIT\n(3-10年中債)", "IEF\n(7-10年中債)", "^IRX\n(國庫券基準)"]
    bar_charts.append({
        "outPath": str(out_dir / "test3_real_bonds_2022_retire.png"),
        "title": f"測試 3：真實美債 ETF（SHY / VGIT / IEF）2022 股債雙殺與 30 年退休表現（選定：{t3['selected_bond_etf']}）",
        "subtitle": "左圖檢視 2022 年激進升息期間之美元單獨回撤與 30/30/40 台幣組合回撤；右圖為 30 年提領破產機率",
        "footnote": "SHY/IEF 上市於 2002-07、VGIT 上市於 2009-11，上市前以 ^IRX/^FVX/^TNX 存續期+凸性公式推算（月相關係數 > 0.94）。",
        "panels": [
            {
                "panelTitle": "左圖：2022 股債雙殺最大回撤（負值越小越痛）",
                "yUnit": "%",
                "categories": b_cats,
                "series": [
                    {
                        "label": "債券單獨2022美元MDD",
                        "colorHex": "#DC2626",
                        "values": [s22[k]["bond_2022_mdd_usd_pct"] for k in b_keys],
                        "valueLabels": [f"{s22[k]['bond_2022_mdd_usd_pct']:.1f}%" for k in b_keys],
                    },
                    {
                        "label": "30/30/40組合2022台幣MDD",
                        "colorHex": "#2563EB",
                        "values": [s22[k]["port_30_30_40_2022_mdd_twd_pct"] for k in b_keys],
                        "valueLabels": [f"{s22[k]['port_30_30_40_2022_mdd_twd_pct']:.1f}%" for k in b_keys],
                    },
                ],
                "yMin": min(min(s22[k]["bond_2022_mdd_usd_pct"] for k in b_keys), min(s22[k]["port_30_30_40_2022_mdd_twd_pct"] for k in b_keys)) - 5.0,
                "yMax": 3.0,
                "refLine": None,
                "refLabel": None,
            },
            {
                "panelTitle": "右圖：30/30/40 退休 30 年資金歸零（破產）機率",
                "yUnit": "%",
                "categories": b_cats,
                "series": [
                    {
                        "label": "全體路徑破產率",
                        "colorHex": "#0D9488",
                        "values": [r30[k]["ruin_prob_pct"] for k in b_keys],
                        "valueLabels": [f"{r30[k]['ruin_prob_pct']:.2f}%" for k in b_keys],
                    },
                    {
                        "label": "剛退休前3年遇大跌破產率",
                        "colorHex": "#EA580C",
                        "values": [r30[k]["early_crash_ruin_prob_pct"] for k in b_keys],
                        "valueLabels": [f"{r30[k]['early_crash_ruin_prob_pct']:.1f}%" for k in b_keys],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(20.0, max(r30[k]["early_crash_ruin_prob_pct"] for k in b_keys) + 6.0),
                "refLine": 5.0,
                "refLabel": "全體破產率警戒 5%",
            },
        ],
    })

    # 4) Test 4: Lifetime Continuous Fan Chart (Ages 30 -> 90, P10 / P50 / P90)
    t4 = res["test_4_lifetime_continuous"]
    q = t4["lifetime_wealth_quantiles_modern"]
    p50_m = [v / 10000.0 for v in q["p50_twd"]]
    p10_m = [v / 10000.0 for v in q["p10_twd"]]
    p90_m = [v / 10000.0 for v in q["p90_twd"]]
    ymax_fan = max(max(p50_m) * 1.4, max(p10_m) * 3.0)
    line_charts.append({
        "outPath": str(out_dir / "test4_lifetime_continuous_fan.png"),
        "title": "測試 4：30 歲一路模擬到 90 歲 — 連續資產分布曲線（含 55–59 歲逐年滑降）",
        "subtitle": f"月投 1 萬元（30→60 歲共投 360 萬）→ 55–59 歲逐年加 8% {t4['selected_bond_etf']} → 60 歲起依 20%/3.5% 保護規則提領（3,000 條路徑）",
        "footnote": f"左圖為 P10 / P50（萬元）；右圖加上 P90 展現長期複利長尾。60 歲資產：P10 {t4['modern_tw_us_bootstrap']['protect_20_35_plus_2']['age60_p10_twd']/10000:.0f}萬 / P50 {t4['modern_tw_us_bootstrap']['protect_20_35_plus_2']['age60_p50_twd']/10000:.0f}萬 / P90 {t4['modern_tw_us_bootstrap']['protect_20_35_plus_2']['age60_p90_twd']/10000:.0f}萬。",
        "panels": [
            {
                "panelTitle": "左圖：差運氣 (P10) 與中位數 (P50) 資產走勢",
                "xLabel": "年齡（歲）",
                "yLabel": "萬元台幣",
                "xValues": [float(a) for a in q["ages"]],
                "series": [
                    {"label": "P50 中位數資產", "colorHex": "#2563EB", "dashed": False, "values": p50_m},
                    {"label": "P10 後 10% 差運氣資產", "colorHex": "#DC2626", "dashed": True, "values": p10_m},
                ],
                "yMin": 0.0,
                "yMax": max(p50_m) * 1.18,
                "vLines": [55.0, 60.0],
                "vLabels": ["55歲滑降", "60歲退休"],
                "hLine": None,
                "hLabel": None,
            },
            {
                "panelTitle": "右圖：完整分布（P10 / P50 / P90，萬元台幣）",
                "xLabel": "年齡（歲）",
                "yLabel": "萬元台幣",
                "xValues": [float(a) for a in q["ages"]],
                "series": [
                    {"label": "P90 好運氣", "colorHex": "#059669", "dashed": True, "values": p90_m},
                    {"label": "P50 中位數", "colorHex": "#2563EB", "dashed": False, "values": p50_m},
                    {"label": "P10 差運氣", "colorHex": "#DC2626", "dashed": False, "values": p10_m},
                ],
                "yMin": 0.0,
                "yMax": max(p90_m) * 1.10,
                "vLines": [55.0, 60.0],
                "vLabels": ["55歲滑降", "60歲退休"],
                "hLine": None,
                "hLabel": None,
            },
        ],
    })

    # 5) Test 5: Withdrawal Protection Rules
    t5 = res["test_5_withdrawal_protection"]
    m5 = t5["modern_30y_bootstrap"]
    s5 = t5["shiller_30y_rolling"]
    bar_charts.append({
        "outPath": str(out_dir / "test5_withdrawal_protection.png"),
        "title": "測試 5：三種提領方式對比（固定 4%+2% vs 20%/3.5% 保護規則 vs 固定 4% 不調整）",
        "subtitle": "左圖對比近代台美抽樣與 Shiller 1871 實質 CPI 之 30 年破產率；右圖檢視最低一年生活費（佔退休首年百分比）",
        "footnote": "固定 4% 不加通膨調整雖幾乎不破產，但在 30 年實際物價通膨下，晚年實質購買力將大幅縮水（詳見右圖紅柱）。",
        "panels": [
            {
                "panelTitle": "左圖：30 年資金歸零（破產）機率對比",
                "yUnit": "%",
                "categories": ["固定 4%\n+2% / CPI", "20% / 3.5%\n保護規則", "固定 4%\n完全不調整"],
                "series": [
                    {
                        "label": "近代台美抽樣(+2%)",
                        "colorHex": "#2563EB",
                        "values": [m5["fixed_4_plus_2"]["ruin_prob_pct"], m5["protect_20_35_plus_2"]["ruin_prob_pct"], m5["flat_4_no_adj"]["ruin_prob_pct"]],
                        "valueLabels": [f"{m5[k]['ruin_prob_pct']:.2f}%" for k in ("fixed_4_plus_2", "protect_20_35_plus_2", "flat_4_no_adj")],
                    },
                    {
                        "label": "Shiller長歷史(實際CPI)",
                        "colorHex": "#DC2626",
                        "values": [s5["fixed_4_cpi"]["ruin_prob_pct"], s5["protect_20_35_cpi"]["ruin_prob_pct"], s5["flat_4_no_adj"]["ruin_prob_pct"]],
                        "valueLabels": [f"{s5[k]['ruin_prob_pct']:.2f}%" for k in ("fixed_4_cpi", "protect_20_35_cpi", "flat_4_no_adj")],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(25.0, s5["fixed_4_cpi"]["ruin_prob_pct"] + 6.0),
                "refLine": 5.0,
                "refLabel": "破產率門檻 5%",
            },
            {
                "panelTitle": "右圖：Shiller 1871 最低一年實質生活費 P10 與第 30 年實質購買力",
                "yUnit": "%",
                "categories": ["固定 4%\n+實際CPI", "20%/3.5%保護\n+實際CPI", "固定 4%\n不調通膨"],
                "series": [
                    {
                        "label": "最低一年實質生活費(P10)",
                        "colorHex": "#0D9488",
                        "values": [s5[k]["min_spending_real_p10_pct"] for k in ("fixed_4_cpi", "protect_20_35_cpi", "flat_4_no_adj")],
                        "valueLabels": [f"{s5[k]['min_spending_real_p10_pct']:.1f}%" for k in ("fixed_4_cpi", "protect_20_35_cpi", "flat_4_no_adj")],
                    },
                    {
                        "label": "第30年實質購買力(中位數)",
                        "colorHex": "#7C3AED",
                        "values": [s5[k]["final_year_real_p50_pct"] for k in ("fixed_4_cpi", "protect_20_35_cpi", "flat_4_no_adj")],
                        "valueLabels": [f"{s5[k]['final_year_real_p50_pct']:.1f}%" for k in ("fixed_4_cpi", "protect_20_35_cpi", "flat_4_no_adj")],
                    },
                ],
                "yMin": 0.0,
                "yMax": 120.0,
                "refLine": 60.0,
                "refLabel": "最低生活費警戒線 60%",
            },
        ],
    })

    # 6) Test 6: Shiller 5 Crisis Paths (Real Wealth Curves)
    t6 = res["test_6_shiller_inflation"]
    fp = t6["five_crisis_paths"]
    colors_5 = {"1929": "#DC2626", "1966": "#EA580C", "1973": "#7C3AED", "2000": "#2563EB", "2008": "#059669"}
    ser_fix = []
    ser_pro = []
    max_y6 = 1500.0
    for yr in ("1929", "1966", "1973", "2000", "2008"):
        cf = [v / 10000.0 for v in fp[yr]["modes"]["fixed_4_cpi"]["real_wealth_curve_twd"]]
        cp = [v / 10000.0 for v in fp[yr]["modes"]["protect_20_35_cpi"]["real_wealth_curve_twd"]]
        max_y6 = max(max_y6, max(cf), max(cp))
        ser_fix.append({"label": f"{yr}年退休", "colorHex": colors_5[yr], "dashed": yr in ("2000", "2008"), "values": cf})
        ser_pro.append({"label": f"{yr}年退休", "colorHex": colors_5[yr], "dashed": yr in ("2000", "2008"), "values": cp})
    line_charts.append({
        "outPath": str(out_dir / "test6_shiller_crisis_paths.png"),
        "title": "測試 6：Shiller 1871–2026 長歷史高通膨 — 五大歷史災難年退休實質資產路徑",
        "subtitle": "起始 1,000 萬實質資產（60/40 股債、台股以美股代理），生活費隨美國實際 CPI 調整；對比固定 4% 與 20%/3.5% 保護規則",
        "footnote": "2000 年退休樣本至 2026 年共 26 年、2008 年共 18 年（未滿 30 年以虛線標示）。1966/1973 年遭遇 1970 年代雙位數停滯性通膨。",
        "panels": [
            {
                "panelTitle": "左圖：固定 4% + 實際 CPI（無保護）實質剩餘資產",
                "xLabel": "退休後年數（年）",
                "yLabel": "實質萬元",
                "xValues": [float(i) for i in range(31)],
                "series": ser_fix,
                "yMin": 0.0,
                "yMax": min(max_y6 * 1.1, 3500.0),
                "vLines": None,
                "vLabels": None,
                "hLine": 1000.0,
                "hLabel": "原始本金 1,000 萬",
            },
            {
                "panelTitle": "右圖：20% / 3.5% 保護規則 + 實際 CPI 實質剩餘資產",
                "xLabel": "退休後年數（年）",
                "yLabel": "實質萬元",
                "xValues": [float(i) for i in range(31)],
                "series": ser_pro,
                "yMin": 0.0,
                "yMax": min(max_y6 * 1.1, 3500.0),
                "vLines": None,
                "vLabels": None,
                "hLine": 1000.0,
                "hLabel": "原始本金 1,000 萬",
            },
        ],
    })

    # 7) Test 7: Large FX Swings Impact
    t7 = res["test_7_fx_swings"]
    sc7 = t7["scenarios"]
    fx_keys = [
        "hist_fx_baseline",
        "pre_retire_to_25",
        "post_retire_to_25",
        "post_retire_to_25_twd20_hedge",
        "pre_retire_to_40",
        "post_retire_to_40",
    ]
    fx_cats = [
        "歷史匯率\n基準",
        "退休前5年\n升值至25",
        "退休後5年\n升值至25",
        "後5年升25\n+20%台幣債",
        "退休前5年\n貶值至40",
        "退休後5年\n貶值至40",
    ]
    bar_charts.append({
        "outPath": str(out_dir / "test7_fx_swings_impact.png"),
        "title": "測試 7：匯率劇烈變動（5 年升至 25 / 貶至 40）對破產機率與台幣生活費衝擊",
        "subtitle": "55 歲 1,000 萬起跑滑降（30/30/40 含 70% 美元資產），比較「退休前 5 年」與「退休後前 5 年」遇匯率急變的結果",
        "footnote": "對照組「+20%台幣債」將 40% 債券拆為 20% 美國公債 ETF + 20% 台幣定存（台幣與美元資產各 50%）。",
        "panels": [
            {
                "panelTitle": "左圖：各匯率情境下 30 年資金歸零（破產）機率",
                "yUnit": "%",
                "categories": fx_cats,
                "series": [
                    {
                        "label": "固定4%+2%",
                        "colorHex": "#DC2626",
                        "values": [sc7[k]["rules"]["fixed_4_plus_2"]["ruin_prob_pct"] for k in fx_keys],
                        "valueLabels": [f"{sc7[k]['rules']['fixed_4_plus_2']['ruin_prob_pct']:.1f}%" for k in fx_keys],
                    },
                    {
                        "label": "20%/3.5%保護",
                        "colorHex": "#2563EB",
                        "values": [sc7[k]["rules"]["protect_20_35_plus_2"]["ruin_prob_pct"] for k in fx_keys],
                        "valueLabels": [f"{sc7[k]['rules']['protect_20_35_plus_2']['ruin_prob_pct']:.1f}%" for k in fx_keys],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(20.0, max(sc7[k]["rules"]["fixed_4_plus_2"]["ruin_prob_pct"] for k in fx_keys) + 5.0),
                "refLine": 10.0,
                "refLabel": "破產率門檻 10%",
            },
            {
                "panelTitle": "右圖：退休第 1 年台幣年生活費中位數（萬元/年）",
                "yUnit": "萬",
                "categories": fx_cats,
                "series": [
                    {
                        "label": "退休首年台幣提領額",
                        "colorHex": "#0D9488",
                        "values": [sc7[k]["rules"]["protect_20_35_plus_2"]["year1_wd_median_twd"] / 10000.0 for k in fx_keys],
                        "valueLabels": [f"{sc7[k]['rules']['protect_20_35_plus_2']['year1_wd_median_twd']/10000:.1f}萬" for k in fx_keys],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(sc7[k]["rules"]["protect_20_35_plus_2"]["year1_wd_median_twd"] / 10000.0 for k in fx_keys) * 1.25,
                "refLine": None,
                "refLabel": None,
            },
        ],
    })

    # 8) Test 8: Different People (Start Ages 25/30/40/45 & Conservative 25/25/50)
    t8 = res["test_8_different_people"]
    p8 = t8["personas_by_start_age"]
    c8 = t8["conservative_comparison"]
    bar_charts.append({
        "outPath": str(out_dir / "test8_different_people_ages.png"),
        "title": "測試 8：不同起始年齡（25/30/40/45 歲）60 歲資產分布 ＋ 保守型 25/25/50 實測對照",
        "subtitle": "左圖比較月投 1 萬（固定 vs 每年加薪 +3%）於 55 歲滑降、60 歲退休的資產中位數；右圖比較保守型 25/25/50 真實債券回測",
        "footnote": f"右圖使用選定債券 {t8['selected_bond_etf']} 實測 1997–2026 全歷史定期定額（每年 1 月再平衡）。",
        "panels": [
            {
                "panelTitle": "左圖：不同起跑年齡 60 歲中位數資產（萬元台幣）",
                "yUnit": "萬",
                "categories": ["25 歲起跑\n(累積35年)", "30 歲起跑\n(累積30年)", "40 歲起跑\n(累積20年)", "45 歲起跑\n(累積15年)"],
                "series": [
                    {
                        "label": "固定月扣1萬",
                        "colorHex": "#2563EB",
                        "values": [p8[f"age_{a}"]["fixed_10k"]["age60_p50_twd"] / 10000.0 for a in (25, 30, 40, 45)],
                        "valueLabels": [f"{p8[f'age_{a}']['fixed_10k']['age60_p50_twd']/10000:.0f}萬" for a in (25, 30, 40, 45)],
                    },
                    {
                        "label": "扣款每年+3%",
                        "colorHex": "#059669",
                        "values": [p8[f"age_{a}"]["grow_3pct_yr"]["age60_p50_twd"] / 10000.0 for a in (25, 30, 40, 45)],
                        "valueLabels": [f"{p8[f'age_{a}']['grow_3pct_yr']['age60_p50_twd']/10000:.0f}萬" for a in (25, 30, 40, 45)],
                    },
                ],
                "yMin": 0.0,
                "yMax": max(p8["age_25"]["grow_3pct_yr"]["age60_p50_twd"] / 10000.0, 1000.0) * 1.18,
                "refLine": 900.0,
                "refLabel": "月領3萬門檻 900萬",
            },
            {
                "panelTitle": "右圖：保守型 25/25/50 vs 60/40 vs 50/50（全歷史 XIRR 與 MDD）",
                "yUnit": "%",
                "categories": ["20/20/60\n超保守", "25/25/50\n保守型", "30/30/40\n退休平衡", "50/50\n一般純股"],
                "series": [
                    {
                        "label": "全歷史XIRR",
                        "colorHex": "#2563EB",
                        "values": [c8[k]["full_history_xirr_pct"] for k in ("cons_20_20_60", "cons_25_25_50", "bal_30_30_40", "eq_50_50")],
                        "valueLabels": [f"{c8[k]['full_history_xirr_pct']:.1f}%" for k in ("cons_20_20_60", "cons_25_25_50", "bal_30_30_40", "eq_50_50")],
                    },
                    {
                        "label": "全歷史最大回撤",
                        "colorHex": "#DC2626",
                        "values": [c8[k]["full_history_mdd_pct"] for k in ("cons_20_20_60", "cons_25_25_50", "bal_30_30_40", "eq_50_50")],
                        "valueLabels": [f"{c8[k]['full_history_mdd_pct']:.1f}%" for k in ("cons_20_20_60", "cons_25_25_50", "bal_30_30_40", "eq_50_50")],
                    },
                ],
                "yMin": -55.0,
                "yMax": 20.0,
                "refLine": -30.0,
                "refLabel": "保守型 −30% 紅線",
            },
        ],
    })

    return {"barCharts": bar_charts, "lineCharts": line_charts}


def render_all_charts(results_path: Path = RESULTS_JSON_PATH, out_dir: Path = BASE_DIR) -> list[Path]:
    res = json.loads(results_path.read_text(encoding="utf-8"))
    payload = build_chart_payloads(res, out_dir)
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        swift_file = td_p / "render_fc.swift"
        json_file = td_p / "payload.json"
        swift_file.write_text(SWIFT_CHART_SOURCE, encoding="utf-8")
        json_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(["swift", str(swift_file), str(json_file)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Swift chart renderer failed:\n{proc.stderr}\n{proc.stdout}")
        print(proc.stdout.strip(), flush=True)
    return [Path(c["outPath"]) for c in payload["barCharts"]] + [Path(c["outPath"]) for c in payload["lineCharts"]]


if __name__ == "__main__":
    render_all_charts()
