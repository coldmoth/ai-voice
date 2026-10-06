// Renders macos/AppIcon.icns: dark glass tile with a voice waveform.
// Usage: swiftc -O scripts/make-icon.swift -o /tmp/make-icon && /tmp/make-icon macos/AppIcon.icns
import AppKit

func render(size: Int) -> Data {
    let s = CGFloat(size)
    let image = NSImage(size: NSSize(width: s, height: s))
    image.lockFocus()
    let ctx = NSGraphicsContext.current!.cgContext
    let inset = s * 0.098
    let tile = CGRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let path = NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.2237, yRadius: tile.width * 0.2237)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -s * 0.012), blur: s * 0.03, color: NSColor.black.withAlphaComponent(0.35).cgColor)
    NSColor.black.setFill(); path.fill()
    ctx.restoreGState()
    path.addClip()
    let gradient = NSGradient(colors: [NSColor(red: 0.16, green: 0.20, blue: 0.30, alpha: 1),
                                       NSColor(red: 0.05, green: 0.06, blue: 0.10, alpha: 1)])!
    gradient.draw(in: tile, angle: -90)
    let glow = NSGradient(colors: [NSColor(red: 0.45, green: 0.70, blue: 1.0, alpha: 0.35), .clear])!
    glow.draw(fromCenter: CGPoint(x: s * 0.5, y: s * 0.78), radius: 0, toCenter: CGPoint(x: s * 0.5, y: s * 0.78), radius: s * 0.6, options: [])
    let heights: [CGFloat] = [0.16, 0.30, 0.46, 0.60, 0.46, 0.30, 0.16]
    let barW = s * 0.062, gap = s * 0.039
    let total = CGFloat(heights.count) * barW + CGFloat(heights.count - 1) * gap
    var x = (s - total) / 2
    let bar = NSGradient(colors: [NSColor(red: 0.80, green: 0.92, blue: 1.0, alpha: 1),
                                  NSColor(red: 0.45, green: 0.72, blue: 1.0, alpha: 1)])!
    for h in heights {
        let rect = CGRect(x: x, y: (s - h * s) / 2, width: barW, height: h * s)
        let r = NSBezierPath(roundedRect: rect, xRadius: barW / 2, yRadius: barW / 2)
        bar.draw(in: r, angle: -90)
        x += barW + gap
    }
    image.unlockFocus()
    let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
    return rep.representation(using: .png, properties: [:])!
}

let output = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.icns"
let dir = NSTemporaryDirectory() + "AppIcon.iconset"
try? FileManager.default.removeItem(atPath: dir)
try! FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    try! render(size: base).write(to: URL(fileURLWithPath: "\(dir)/icon_\(base)x\(base).png"))
    try! render(size: base * 2).write(to: URL(fileURLWithPath: "\(dir)/icon_\(base)x\(base)@2x.png"))
}
let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
task.arguments = ["-c", "icns", dir, "-o", output]
try! task.run(); task.waitUntilExit()
exit(task.terminationStatus)
