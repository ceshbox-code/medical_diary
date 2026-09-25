/*
 * Декодирование штрихкода/DataMatrix упаковки лекарства прямо в браузере.
 *
 * Требует, чтобы ДО этого файла был подключён static/js/vendor/zxing.min.js
 * (UMD-сборка пакета @zxing/library, даёт глобальный объект window.ZXing).
 * Ничего не отправляется на сервер и никуда не сохраняется — только сам
 * декодированный текст кода превращается в GTIN и возвращается вызывающему
 * коду (meds.js), который сам решает, что с ним делать.
 *
 * ИИ/сервис не проверяет подлинность лекарства и не обращается к ИС МДЛП/
 * «Честный знак» — читается только идентификатор применения 01 (GTIN),
 * серийный номер и криптохвост игнорируются: они для этой задачи не нужны.
 */
(function () {
  'use strict';

  var activeReader = null;
  var activeVideoId = null;

  // reset() из библиотеки останавливает только внутренний цикл декодирования,
  // но не всегда по-настоящему освобождает MediaStream — на iOS Safari это
  // приводило к тому, что второй скан подряд не мог получить камеру (поток от
  // первого раза формально ещё "жив"). Поэтому останавливаем треки сами,
  // не полагаясь только на библиотеку.
  function releaseCamera() {
    if (activeReader) {
      try { activeReader.reset(); } catch (e) { /* не критично */ }
      activeReader = null;
    }
    if (activeVideoId) {
      var el = document.getElementById(activeVideoId);
      if (el && el.srcObject && typeof el.srcObject.getTracks === 'function') {
        el.srcObject.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* не критично */ } });
        el.srcObject = null;
      }
      activeVideoId = null;
    }
  }

  function extractGtin(raw) {
    var s = String(raw || '');
    // GS1 DataMatrix: AI 01 (GTIN) идёт первым и всегда фиксированной
    // длины — 14 цифр, поэтому его можно выделить простым поиском.
    var m = s.match(/01(\d{14})/);
    if (m) { return m[1]; }
    var digits = s.replace(/\D/g, '');
    if (digits.length === 14) { return digits; }
    if (digits.length === 13) { return '0' + digits; }    // EAN-13 -> GTIN-14
    if (digits.length === 12) { return '00' + digits; }   // UPC-A -> GTIN-14
    if (digits.length === 8) { return '000000' + digits; } // EAN-8 -> GTIN-14
    return null;
  }

  function scanOnce(videoElementId) {
    if (!window.ZXing) {
      return Promise.reject(new Error('Библиотека сканирования не загружена'));
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error('Камера недоступна в этом браузере'));
    }
    // На всякий случай гасим предыдущую камеру, если она вдруг ещё не была
    // отпущена (например, предыдущий вызов не успел корректно завершиться).
    releaseCamera();

    var hints = new Map();
    hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      window.ZXing.BarcodeFormat.EAN_13,
      window.ZXing.BarcodeFormat.EAN_8,
      window.ZXing.BarcodeFormat.UPC_A,
      window.ZXing.BarcodeFormat.CODE_128,
      window.ZXing.BarcodeFormat.DATA_MATRIX,
      window.ZXing.BarcodeFormat.QR_CODE
    ]);
    // QR (и DataMatrix) требуют заметно более чёткого и крупного кадра,
    // чем 1D-штрихкод. TRY_HARDER заставляет декодер разбирать кадр
    // тщательнее (дороже по CPU, но это разовый скан, а не видео-поток).
    hints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
    var reader = new window.ZXing.BrowserMultiFormatReader(hints);
    activeReader = reader;
    activeVideoId = videoElementId;
    // Явно просим у камеры разрешение повыше и заднюю камеру — браузер по
    // умолчанию может выдать поток низкого разрешения, которого хватает
    // для штрихкода, но не хватает для плотного QR/DataMatrix.
    var constraints = {
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1280 },
        height: { ideal: 720 },
        advanced: [{ focusMode: 'continuous' }]
      }
    };
    var decodePromise = (typeof reader.decodeOnceFromConstraints === 'function')
      ? reader.decodeOnceFromConstraints(constraints, videoElementId)
      : reader.decodeOnceFromVideoDevice(undefined, videoElementId);

    // Диагностика: какое разрешение камера реально согласовала (а не что мы
    // попросили в constraints — это лишь пожелание, слабая камера отдаст
    // максимум, что умеет). Видно только в консоли браузера.
    var videoEl = document.getElementById(videoElementId);
    if (videoEl) {
      var logRealResolution = function () {
        if (videoEl.videoWidth) {
          console.debug('[BarcodeScan] реальное разрешение камеры:', videoEl.videoWidth + 'x' + videoEl.videoHeight);
          videoEl.removeEventListener('loadedmetadata', logRealResolution);
        }
      };
      videoEl.addEventListener('loadedmetadata', logRealResolution);
    }

    return decodePromise.then(
      function (result) {
        releaseCamera();
        var gtin = extractGtin(result.text);
        // Диагностика: видно в консоли браузера (F12 -> Console), никуда не
        // отправляется. Помогает понять, что именно прочитала камера, если
        // код найден, но лекарство не определилось по базе.
        console.debug('[BarcodeScan] сырой текст:', result.text, '-> GTIN:', gtin);
        if (!gtin) { throw new Error('Не удалось определить код товара'); }
        return gtin;
      },
      function (err) {
        releaseCamera();
        throw err;
      }
    );
  }

  // Вызывается извне (meds.js) при закрытии модалки сканирования любым
  // способом (кнопка «Отмена», клик по фону, Escape) — гасит камеру, даже
  // если сканирование ещё не завершилось.
  function cancel() {
    releaseCamera();
  }

  window.BarcodeScan = { scanOnce: scanOnce, cancel: cancel, extractGtin: extractGtin };
})();
