/**
 * Extraction DOM quiz Udemy / Odoo — utilisé par le favori pleine page.
 */
(function (global) {
  'use strict';

  var IMAGE_HINTS = (
    'following warning|following message|following dialog|following screenshot|' +
    'following image|shown below|displayed below|this button|this warning|' +
    'what does this|when does odoo show|capture d|screenshot|tableau|trial balance'
  ).split('|');

  function textOf(el) {
    return (el && (el.innerText || el.textContent) || '').replace(/\s+/g, ' ').trim();
  }

  function titleNeedsImage(title) {
    var t = (title || '').toLowerCase();
    if (!t) return false;
    for (var i = 0; i < IMAGE_HINTS.length; i++) {
      if (t.indexOf(IMAGE_HINTS[i]) >= 0) return true;
    }
    return false;
  }

  function blockHasMeaningfulImage(block) {
    if (!block) return false;
    var imgs = block.querySelectorAll('img');
    for (var i = 0; i < imgs.length; i++) {
      var im = imgs[i];
      var w = im.naturalWidth || im.width || 0;
      var h = im.naturalHeight || im.height || 0;
      if (w >= 80 && h >= 40) return true;
    }
    return !!block.querySelector('canvas, svg[width], table');
  }

  function labelForRadio(r) {
    if (!r) return '';
    var id = r.id;
    if (id) {
      var lab = document.querySelector('label[for="' + id.replace(/"/g, '\\"') + '"]');
      if (lab) return textOf(lab);
    }
    var parentLab = r.closest('label');
    if (parentLab) return textOf(parentLab);
    return textOf(r.parentElement);
  }

  function findQuestionBlock(input) {
    var el = input.parentElement;
    for (var i = 0; i < 14 && el; i++) {
      var radios = el.querySelectorAll('input[type="radio"]');
      if (radios.length >= 2) return el;
      el = el.parentElement;
    }
    return input.closest('form, article, section, main') || document.body;
  }

  function titleFromBlock(block, radios) {
    var cands = block.querySelectorAll(
      'h1, h2, h3, h4, h5, h6, legend, [data-purpose="render-safe-html"], .question, .o_wslides_lesson_content > p'
    );
    var i, t, best = '';
    for (i = 0; i < cands.length; i++) {
      t = textOf(cands[i]);
      if (t.length > best.length && t.length > 12) best = t;
    }
    if (best) return best;
    var lines = textOf(block).split(/\n+/);
    for (i = 0; i < lines.length; i++) {
      t = lines[i].trim();
      if (t.length > 20 && t.indexOf('?') >= 0) return t;
    }
    return lines[0] || '';
  }

  function extractFromRadioGroups() {
    var groups = new Map();
    document.querySelectorAll('input[type="radio"]').forEach(function (r) {
      var name = r.name || r.getAttribute('name') || '';
      if (!name) return;
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(r);
    });
    var items = [];
    groups.forEach(function (radios) {
      if (radios.length < 2) return;
      var block = findQuestionBlock(radios[0]);
      var title = titleFromBlock(block, radios);
      var answers = [];
      var seen = new Set();
      radios.forEach(function (r) {
        var a = labelForRadio(r);
        if (!a || seen.has(a)) return;
        seen.add(a);
        answers.push(a);
      });
      if (answers.length < 2) return;
      var needImg = titleNeedsImage(title) || blockHasMeaningfulImage(block);
      var ci = null;
      var ciVis = false;
      radios.forEach(function (r, idx) {
        if (r.checked) {
          ciVis = true;
          ci = idx + 1;
        }
      });
      items.push({
        title: title,
        answers: answers,
        correct_index: ciVis ? ci : null,
        correct_index_visible: ciVis,
        explication_udemy: '',
        needs_question_image: needImg,
        crop_rel: null,
      });
    });
    return items;
  }

  function extractUdemy() {
    var root = document.querySelector('[data-purpose="quiz-question-container"]');
    if (!root) return [];
    var titleEl = root.querySelector('[data-purpose="render-safe-html"]');
    var title = textOf(titleEl);
    var answers = [];
    root.querySelectorAll('[data-purpose="quiz-option"]').forEach(function (opt) {
      var t = textOf(opt);
      if (t) answers.push(t);
    });
    if (!title || answers.length < 2) return [];
    return [
      {
        title: title,
        answers: answers,
        correct_index: null,
        correct_index_visible: false,
        explication_udemy: '',
        needs_question_image: titleNeedsImage(title) || blockHasMeaningfulImage(root),
        crop_rel: null,
      },
    ];
  }

  function extract(host) {
    var h = (host || location.hostname || '').toLowerCase();
    var items = [];
    if (h.indexOf('udemy') >= 0) {
      items = extractUdemy();
    }
    if (items.length < 1) {
      items = extractFromRadioGroups();
    }
    if (items.length > 4) items = items.slice(0, 4);
    return { items: items, page_host: host || location.hostname || '' };
  }

  global.QuizDomExtract = { extract: extract };
})(typeof window !== 'undefined' ? window : global);
