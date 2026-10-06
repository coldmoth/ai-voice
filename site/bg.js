// Site background: slow blurred blobs on a canvas.
// Parameters copied from macos/desktop/onboarding.js (PAL, SPEED, PARALLAX, EASE, draw math).
(function () {
  const PAL = ['#3d7bff', '#9b5cff', '#19d3c5', '#ff5fa2'];
  const SPEED = 0.3, PARALLAX = 0.12, EASE = 0.02;
  const bg = {W: 0, H: 0, mx: .5, my: .5, sx: .5, sy: .5, t: 0, raf: 0, resizing: false};
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const canvas = () => document.getElementById('bg-canvas');

  function bgFit() {
    // The blobs are soft, so a quarter of the CSS size is enough; full Retina resolution starved the main thread.
    const cv = canvas();
    bg.W = cv.width = Math.max(1, Math.round(cv.clientWidth / 4));
    bg.H = cv.height = Math.max(1, Math.round(cv.clientHeight / 4));
  }
  function bgDraw(animate) {
    const ctx = canvas().getContext('2d'), {W, H} = bg;
    if (animate) { bg.t += .016 * SPEED; bg.sx += (bg.mx - bg.sx) * EASE; bg.sy += (bg.my - bg.sy) * EASE; }
    const time = bg.t;
    ctx.globalCompositeOperation = 'source-over'; ctx.fillStyle = '#05070d'; ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = 'lighter';
    for (let i = 0; i < 5; i++) {
      const x = W * (.5 + .38 * Math.sin(time * .9 + i * 1.7) + (bg.sx - .5) * PARALLAX * (i % 2 ? 1 : -1));
      const y = H * (.5 + .35 * Math.cos(time * .7 + i * 2.3) + (bg.sy - .5) * PARALLAX);
      const r = Math.min(W, H) * (.5 + .08 * Math.sin(time * 1.3 + i));
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, PAL[i % 4] + '8c'); g.addColorStop(1, PAL[i % 4] + '00');
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    }
  }
  function bgFrame() {
    bg.raf = 0;
    if (document.hidden) return;
    bgDraw(true);
    bg.raf = requestAnimationFrame(bgFrame);
  }
  function bgStart() {
    bgFit();
    if (reduced()) { bgDraw(false); return; }
    if (!bg.raf) bg.raf = requestAnimationFrame(bgFrame);
  }

  window.addEventListener('mousemove', e => {
    bg.mx = e.clientX / window.innerWidth; bg.my = e.clientY / window.innerHeight;
  });
  window.addEventListener('resize', () => {
    if (bg.resizing) return;
    bg.resizing = true;
    requestAnimationFrame(() => { bg.resizing = false; bgFit(); if (!bg.raf) bgDraw(false); });
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !reduced() && !bg.raf) bg.raf = requestAnimationFrame(bgFrame);
  });
  window.matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', bgStart);

  bgStart();
})();
