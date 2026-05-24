/**
 * Capture pleine page Udemy / Odoo — chargé via favori depuis /import-capture.
 * html2canvas sur la zone quiz (scroll inclus) → postMessage vers l’onglet Capture ou presse-papiers.
 */
(function () {
  'use strict';
  if (window.__odooQuizFullPageRunning) return;
  window.__odooQuizFullPageRunning = true;

  var script = document.currentScript;
  var captureOrigin = '';
  if (script && script.dataset && script.dataset.odooQuizOrigin) {
    captureOrigin = script.dataset.odooQuizOrigin;
  } else if (script && script.src) {
    try {
      captureOrigin = new URL(script.src, window.location.href).origin;
    } catch (eOrigin) {
      captureOrigin = '';
    }
  }

  var MAX_EDGE = 4096;

  function showStatus(msg, isErr) {
    var el = document.getElementById('odoo-quiz-fp-status');
    if (!el) {
      el = document.createElement('div');
      el.id = 'odoo-quiz-fp-status';
      el.setAttribute('role', 'status');
      el.style.cssText =
        'position:fixed;z-index:2147483647;top:12px;left:50%;transform:translateX(-50%);' +
        'max-width:min(92vw,520px);padding:10px 16px;border-radius:8px;' +
        'font:14px/1.45 system-ui,-apple-system,sans-serif;box-shadow:0 4px 24px rgba(0,0,0,.22);';
      document.body.appendChild(el);
    }
    el.style.background = isErr ? '#fef2f2' : '#ecfdf5';
    el.style.color = isErr ? '#991b1b' : '#065f46';
    el.style.border = isErr ? '1px solid #fecaca' : '1px solid #a7f3d0';
    el.textContent = msg;
  }

  function removeStatusLater(ms) {
    setTimeout(function () {
      var n = document.getElementById('odoo-quiz-fp-status');
      if (n) n.remove();
    }, ms || 4500);
  }

  function inferCaptureSource() {
    var fromDataset =
      script && script.dataset && script.dataset.odooQuizCaptureSource
        ? script.dataset.odooQuizCaptureSource
        : '';
    if (fromDataset === 'odoo' || fromDataset === 'udemy') return fromDataset;
    var h = (window.location.hostname || '').toLowerCase();
    if (h.indexOf('odoo.com') >= 0 || h.indexOf('odoo') >= 0) return 'odoo';
    if (h.indexOf('udemy.com') >= 0) return 'udemy';
    return 'udemy';
  }

  function findCaptureRoot() {
    var selectors = [
      '[data-purpose="quiz-question-container"]',
      '.quiz--question-container',
      '#quiz-screen',
      '.quiz-container',
      '[data-purpose="curriculum-item-viewer-content"]',
      'main.content-area',
      '.o_wslides_lesson_content',
      '#wrapwrap',
      'main',
      '[role="main"]',
      'article',
      'body',
    ];
    var i, el, best = document.body;
    var bestH = 0;
    for (i = 0; i < selectors.length; i++) {
      el = document.querySelector(selectors[i]);
      if (!el) continue;
      var h = Math.max(el.scrollHeight || 0, el.offsetHeight || 0);
      if (h > bestH) {
        bestH = h;
        best = el;
      }
    }
    return best;
  }

  function downscaleCanvas(src, maxEdge) {
    var w = src.width;
    var h = src.height;
    var m = Math.max(w, h);
    if (m <= maxEdge) return src;
    var scale = maxEdge / m;
    var nw = Math.max(1, Math.round(w * scale));
    var nh = Math.max(1, Math.round(h * scale));
    var out = document.createElement('canvas');
    out.width = nw;
    out.height = nh;
    var ctx = out.getContext('2d');
    if (ctx) ctx.drawImage(src, 0, 0, nw, nh);
    return out;
  }

  /** Odoo / Tailwind utilisent color() CSS4 — html2canvas 1.4 échoue ; html2canvas-pro les gère. */
  function sanitizeClonedDocForCapture(clonedDoc) {
    var win = clonedDoc.defaultView;
    if (!win) return;
    var colorFn = /\bcolor\s*\([^)]*\)/gi;
    clonedDoc.querySelectorAll('style').forEach(function (node) {
      if (node.textContent) node.textContent = node.textContent.replace(colorFn, '#212529');
    });
    clonedDoc.querySelectorAll('link[rel="stylesheet"]').forEach(function (node) {
      node.parentNode && node.parentNode.removeChild(node);
    });
    clonedDoc.querySelectorAll('*').forEach(function (el) {
      try {
        var cs = win.getComputedStyle(el);
        if (cs.color && /\bcolor\s*\(/i.test(cs.color)) el.style.color = '#212529';
        if (cs.backgroundColor && /\bcolor\s*\(/i.test(cs.backgroundColor)) {
          el.style.backgroundColor = '#ffffff';
        }
      } catch (eSt) {
        /* ignore */
      }
    });
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = url;
      s.crossOrigin = 'anonymous';
      s.onload = function () {
        resolve();
      };
      s.onerror = function () {
        reject(new Error('Script indisponible : ' + url));
      };
      document.head.appendChild(s);
    });
  }

  function loadDomExtract(origin) {
    if (window.QuizDomExtract) return Promise.resolve(window.QuizDomExtract);
    return loadScript(origin + '/static/quiz_dom_extract.js?v=' + Date.now()).then(function () {
      if (!window.QuizDomExtract) throw new Error('QuizDomExtract absent');
      return window.QuizDomExtract;
    });
  }

  function loadHtml2Canvas() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src =
        'https://cdn.jsdelivr.net/npm/html2canvas-pro@1.5.8/dist/html2canvas-pro.min.js';
      s.crossOrigin = 'anonymous';
      s.onload = function () {
        if (window.html2canvas) resolve(window.html2canvas);
        else reject(new Error('html2canvas-pro non chargé'));
      };
      s.onerror = function () {
        reject(new Error('Impossible de charger html2canvas-pro (réseau ou blocage)'));
      };
      document.head.appendChild(s);
    });
  }

  function dataUrlToBlob(dataUrl) {
    var parts = dataUrl.split(',');
    var mime = (parts[0].match(/:(.*?);/) || [])[1] || 'image/png';
    var bin = atob(parts[1] || '');
    var len = bin.length;
    var u8 = new Uint8Array(len);
    while (len--) u8[len] = bin.charCodeAt(len);
    return new Blob([u8], { type: mime });
  }

  async function run() {
    try {
      showStatus('Capture pleine page… restez sur cet onglet quelques secondes.', false);
      window.scrollTo(0, 0);
      await new Promise(function (r) {
        setTimeout(r, 400);
      });

      var h2c = await loadHtml2Canvas();
      var root = findCaptureRoot();
      var fullW = Math.max(
        document.documentElement.scrollWidth || 0,
        root.scrollWidth || 0,
        root.offsetWidth || 0
      );
      var fullH = Math.max(
        document.documentElement.scrollHeight || 0,
        root.scrollHeight || 0,
        root.offsetHeight || 0
      );
      var scale = Math.min(2, window.devicePixelRatio || 1.5);

      var canvas = await h2c(root, {
        backgroundColor: '#ffffff',
        scale: scale,
        useCORS: true,
        allowTaint: true,
        logging: false,
        width: fullW,
        height: fullH,
        windowWidth: fullW,
        windowHeight: fullH,
        scrollX: 0,
        scrollY: 0,
        x: 0,
        y: 0,
        onclone: sanitizeClonedDocForCapture,
      });

      canvas = downscaleCanvas(canvas, MAX_EDGE);
      var pageHost = window.location.hostname || '';
      var domItems = [];
      try {
        var domLib = await loadDomExtract(captureOrigin);
        var domOut = domLib.extract(pageHost);
        if (domOut && domOut.items) domItems = domOut.items;
      } catch (domErr) {
        domItems = [];
      }

      // crop_rel = boîte de l'image dans la capture (repère root, fullW×fullH,
      // scroll remis à 0 plus haut). Sert de repli quand il n'y a pas d'URL d'image.
      try {
        var rRect = root.getBoundingClientRect();
        for (var di = 0; di < domItems.length; di++) {
          var it = domItems[di];
          if (!it) continue;
          var el = it._imgEl;
          if (el && el.getBoundingClientRect && fullW > 0 && fullH > 0) {
            var iRect = el.getBoundingClientRect();
            var cl = (iRect.left - rRect.left) / fullW;
            var ct = (iRect.top - rRect.top) / fullH;
            var cw = iRect.width / fullW;
            var ch = iRect.height / fullH;
            if (cw > 0.02 && ch > 0.02 && cl > -0.05 && ct > -0.05 && cl < 1 && ct < 1) {
              it.crop_rel = {
                left: Math.max(0, cl),
                top: Math.max(0, ct),
                width: Math.min(1, cw),
                height: Math.min(1, ch)
              };
            }
          }
        }
      } catch (cropErr) {
        /* crop_rel reste null -> repli capture entière côté serveur */
      }
      // Toujours retirer la réf. DOM (non sérialisable) avant l'envoi JSON.
      for (var dj = 0; dj < domItems.length; dj++) {
        if (domItems[dj]) {
          try { delete domItems[dj]._imgEl; } catch (eDel) { domItems[dj]._imgEl = null; }
        }
      }

      var blob = await new Promise(function (res, rej) {
        canvas.toBlob(
          function (b) {
            if (b) res(b);
            else rej(new Error('export PNG'));
          },
          'image/png',
          0.88
        );
      });

      var uploadPayload = null;
      if (captureOrigin) {
        try {
          var fd = new FormData();
          fd.append('image', blob, 'capture.png');
          fd.append('page_host', pageHost);
          if (domItems.length) {
            fd.append('dom_json', JSON.stringify(domItems));
          }
          var up = await fetch(captureOrigin + '/import-capture/fullpage', {
            method: 'POST',
            body: fd,
            mode: 'cors',
            credentials: 'omit',
          });
          if (up.ok) {
            uploadPayload = await up.json();
          }
        } catch (upErr) {
          uploadPayload = null;
        }
      }

      if (uploadPayload && uploadPayload.id) {
        var captureUrl =
          captureOrigin + '/import-capture?fullpage=' + encodeURIComponent(uploadPayload.id);
        if (window.opener && !window.opener.closed) {
          try {
            window.opener.focus();
          } catch (fErr) {
            /* ignore */
          }
          window.opener.postMessage(
            { type: 'odoo-quiz-fullpage-capture', uploadId: uploadPayload.id },
            captureOrigin
          );
          showStatus('Capture envoyée — analyse sur l’onglet Capture.', false);
          removeStatusLater(4000);
          return;
        }
        window.open(captureUrl, 'odoo_capture_udemy');
        showStatus('Onglet Capture ouvert — analyse en cours.', false);
        removeStatusLater(5000);
        return;
      }

      if (captureOrigin && window.opener && !window.opener.closed) {
        try {
          var dataUrl = canvas.toDataURL('image/jpeg', 0.82);
          if (dataUrl.length < 1200000) {
            window.opener.focus();
            window.opener.postMessage(
              { type: 'odoo-quiz-fullpage-capture', dataUrl: dataUrl },
              captureOrigin
            );
            showStatus('Capture envoyée — analyse sur l’onglet Capture.', false);
            removeStatusLater(4000);
            return;
          }
        } catch (pmErr) {
          /* ignore */
        }
      }

      if (navigator.clipboard && window.ClipboardItem) {
        try {
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
          showStatus(
            'Image copiée — onglet Capture : cliquez la zone puis Cmd+V / Ctrl+V.',
            false
          );
          removeStatusLater(6000);
          return;
        } catch (clipErr) {
          /* fallback */
        }
      }

      var fallback = blob;
      var url = URL.createObjectURL(fallback);
      window.open(url, '_blank');
      showStatus(
        'Ouvrez l’image, copiez-la, puis collez dans odoo-quiz (zone de collage).',
        false
      );
      removeStatusLater(8000);
    } catch (err) {
      var msg = err && err.message ? String(err.message) : String(err);
      showStatus(
        'Échec capture pleine page : ' +
          msg +
          ' — essayez zoom arrière + partage d’onglet, ou une extension « full page screenshot ».',
        true
      );
    } finally {
      window.__odooQuizFullPageRunning = false;
    }
  }

  run();
})();
