BOX FLIP AUTOMATOR v1.9 — macOS TAHOE BUILD

WHAT THIS IS
A macOS port of Box Flip Automator Windows v1.9. It keeps the existing:
- Normal mode
- Across the Line mode (Left -> Middle -> Right)
- Random Lines mode (random column per row, top -> bottom)
- Wager entry + green confirmation + blue Done detection
- Screen-region drag/highlight setup
- Odds weighting
- MLB 5yr odds calibration
- Emergency stop with Esc or the top-left mouse corner

MAC-SPECIFIC IMPLEMENTATION
- Retina-safe screen capture uses Pillow 12.3 scale_down mode so screenshot coordinates match click coordinates.
- Native Quartz/CoreGraphics events move/click the mouse and type wager values.
- Apple Vision OCR reads odds; Tesseract is NOT required.
- macOS Screen Recording and Accessibility permissions are checked from inside the app.

WHAT YOUR FRIEND INSTALLS
Nothing extra. Send the finished .dmg from GitHub Actions.
1. Open the DMG.
2. Drag Box Flip Automator into Applications.
3. Open the app.
4. Click "Mac permissions" and allow:
   - Screen & System Audio Recording
   - Accessibility
5. If macOS asks, quit/reopen the app once.

FIRST TEST BUILD / GATEKEEPER
The included workflow produces an ad-hoc signed test build. On the first launch,
macOS may block it because it is not notarized with an Apple Developer ID.
If that happens, attempt to open it once, then use System Settings -> Privacy & Security -> Open Anyway.
This is still much simpler for the recipient than installing Python or developer tools.

APPLE SILICON VS INTEL
GitHub Actions builds BOTH:
- BoxFlipAutomator-macOS-Tahoe-AppleSilicon.dmg  (M1/M2/M3/M4/M5 etc.)
- BoxFlipAutomator-macOS-Tahoe-Intel.dmg         (Intel MacBook Pro)
Check Apple menu -> About This Mac if you are unsure which one to send.

HOW TO BUILD FROM WINDOWS USING GITHUB
1. Create a GitHub repository (private is fine).
2. Upload the CONTENTS of this folder, including the hidden .github folder.
3. Open the repository's Actions tab.
4. Choose "Build Box Flip Automator for macOS Tahoe".
5. Click "Run workflow".
6. When both jobs finish, download the two DMG artifacts.
7. Send your friend the DMG matching his Mac.

OPTIONAL PROFESSIONAL DISTRIBUTION
For a warning-free download experience, add an Apple Developer ID Application
certificate and notarization credentials to the GitHub workflow later. The app
itself does not require the Mac App Store.

IMPORTANT TEST NOTE
This package was source-tested in the current environment, but actual Quartz,
Vision, TCC permissions, and click coordinates must be validated on a real Mac.
The first Mac test should use a harmless page and a tiny number of rounds.
