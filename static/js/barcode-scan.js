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
 *
 * АРХИТЕКТУРА (важно, почему именно так):
 *
 * 1) Камера запрашивается ОДИН РАЗ за сессию страницы и переиспользуется
 *    между сканами (module-level sharedStream). Повторные вызовы
 *    getUserMedia() в одной вкладке — известный баг WebKit, после
 *    нескольких запросов камера может перестать выдавать кадры вообще:
 *    https://bugs.webkit.org/show_bug.cgi?id=204106
 *    Поток по-настоящему останавливается только при закрытии страницы.
 *
 * 2) Декодируем не весь кадр видео, а только вырезанный квадрат по центру
 *    (область прицеливания) — декодеру физически меньше пикселей разбирать,
 *    это прямо ускоряет распознавание на плотных кодах (DataMatrix). Метода
 *    "decodeFromCanvas" в этой сборке нет, поэтому используется низкоуровневая
 *    связка, которой сама библиотека пользуется внутри себя:
 *    HTMLCanvasElementLuminanceSource -> BinaryBitmap(HybridBinarizer) ->
 *    MultiFormatReader.decode(...). Собственный цикл опроса (setTimeout),
 *    а не decodeOnce* — те методы либо не дают доступа к обрезке кадра,
 *    либо (см. историю с decodeOnceFromConstraints/reset()) слишком
 *    непредсказуемо ведут себя при попытке прервать/перезапустить их
 *    посреди работы.
 */
(function () {
  'use strict';

  var sharedStream = null;
  var reader = null;      // один и тот же MultiFormatReader на все сканы — он не хранит состояния потока/камеры
  var readerHints = null;
  var currentScan = null; // { timer, canvas, ctx, videoEl, reject } — состояние активного цикла опроса, если есть

  var CROP_FRACTION = 0.80;   // доля меньшей стороны кадра, которую вырезаем под область прицеливания
  var CROP_CANVAS_SIZE = 360; // сторона рабочего canvas в пикселях — для декодирования этого достаточно
  var POLL_INTERVAL_MS = 130;

  function streamIsLive(stream) {
    return !!stream && stream.getVideoTracks().some(function (t) { return t.readyState === 'live'; });
  }

  function getSharedStream(constraints) {
    if (streamIsLive(sharedStream)) {
      return Promise.resolve(sharedStream);
    }
    return navigator.mediaDevices.getUserMedia(constraints).then(function (stream) {
      sharedStream = stream;
      return stream;
    });
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

  function getReader() {
    if (!reader) {
      reader = new window.ZXing.MultiFormatReader();
      readerHints = new Map();
      readerHints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, [
        window.ZXing.BarcodeFormat.EAN_13,
        window.ZXing.BarcodeFormat.EAN_8,
        window.ZXing.BarcodeFormat.UPC_A,
        window.ZXing.BarcodeFormat.CODE_128,
        window.ZXing.BarcodeFormat.DATA_MATRIX,
        window.ZXing.BarcodeFormat.QR_CODE
      ]);
      readerHints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);
      if (typeof reader.setHints === 'function') { reader.setHints(readerHints); }
    }
    return reader;
  }

  // Декодирует один кадр из canvas. Бросает исключение, если код не найден —
  // это нормальное, ожидаемое состояние почти на каждом кадре, а не ошибка.
  function decodeCanvasOnce(canvas) {
    var r = getReader();
    var luminanceSource = new window.ZXing.HTMLCanvasElementLuminanceSource(canvas);
    var binaryBitmap = new window.ZXing.BinaryBitmap(new window.ZXing.HybridBinarizer(luminanceSource));
    return r.decode(binaryBitmap, readerHints);
  }

  function stopCurrentScan() {
    if (currentScan) {
      clearTimeout(currentScan.timer);
      currentScan = null;
    }
  }

  // По-настоящему гасит камеру целиком. Вызывается только при закрытии
  // страницы — не после каждого скана.
  function stopSharedStreamFully() {
    stopCurrentScan();
    if (sharedStream) {
      sharedStream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* не критично */ } });
      sharedStream = null;
    }
  }
  window.addEventListener('pagehide', stopSharedStreamFully);

  function scanOnce(videoElementId) {
    if (!window.ZXing) {
      return Promise.reject(new Error('Библиотека сканирования не загружена'));
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject(new Error('Камера недоступна в этом браузере'));
    }
    var videoEl = document.getElementById(videoElementId);
    if (!videoEl) {
      return Promise.reject(new Error('Не найден элемент видео для скана'));
    }
    stopCurrentScan(); // на случай, если предыдущий скан почему-то не был остановлен снаружи

    // Явно просим у камеры разрешение повыше и заднюю камеру — браузер по
    // умолчанию может выдать поток низкого разрешения, которого хватает
    // для штрихкода, но не хватает для плотного QR/DataMatrix. Реально
    // применяется только при первом (настоящем) getUserMedia за сессию —
    // при переиспользовании потока constraints уже не действуют, это
    // ожидаемо (см. комментарий в начале файла).
    var constraints = {
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1280 },
        height: { ideal: 720 },
        advanced: [{ focusMode: 'continuous' }]
      }
    };

    return getSharedStream(constraints).then(function (stream) {
      if (videoEl.srcObject !== stream) {
        videoEl.srcObject = stream;
      }
      var playResult = videoEl.play();
      if (playResult && typeof playResult.catch === 'function') {
        playResult.catch(function () { /* "already playing" и т.п. — не критично */ });
      }

      return new Promise(function (resolve, reject) {
        var canvas = document.createElement('canvas');
        canvas.width = CROP_CANVAS_SIZE;
        canvas.height = CROP_CANVAS_SIZE;
        var ctx = canvas.getContext('2d', { willReadFrequently: true });

        var loggedResolutionOnce = false;

        function tick() {
          if (videoEl.readyState < 2 || !videoEl.videoWidth) {
            currentScan.timer = setTimeout(tick, POLL_INTERVAL_MS);
            return;
          }
          if (!loggedResolutionOnce) {
            loggedResolutionOnce = true;
            console.debug('[BarcodeScan] реальное разрешение камеры:', videoEl.videoWidth + 'x' + videoEl.videoHeight);
          }

          var side = Math.min(videoEl.videoWidth, videoEl.videoHeight) * CROP_FRACTION;
          var sx = (videoEl.videoWidth - side) / 2;
          var sy = (videoEl.videoHeight - side) / 2;
          ctx.drawImage(videoEl, sx, sy, side, side, 0, 0, CROP_CANVAS_SIZE, CROP_CANVAS_SIZE);

          try {
            var result = decodeCanvasOnce(canvas);
            var text = (typeof result.getText === 'function') ? result.getText() : result.text;
            var gtin = extractGtin(text);
            if (gtin) {
              console.debug('[BarcodeScan] сырой текст:', text, '-> GTIN:', gtin);
              currentScan = null;
              resolve(gtin);
              return;
            }
            // Текст декодировался, но не похож на GTIN — редкий случай
            // (например, отсканировали что-то постороннее), пробуем дальше.
          } catch (e) {
            // Ожидаемо почти на каждом кадре, пока код не попал в кадр
            // ровно и чётко — не логируем, чтобы не спамить консоль.
          }
          currentScan.timer = setTimeout(tick, POLL_INTERVAL_MS);
        }

        currentScan = { timer: null, reject: reject };
        tick();
      });
    });
  }

  // Вызывается извне (meds.js) при закрытии модалки сканирования любым
  // способом (кнопка «Отмена», клик по фону, Escape) — останавливает цикл
  // опроса. Сам поток камеры не гасит намеренно — см. комментарий в начале
  // файла про баг WebKit с повторным getUserMedia.
  function cancel() {
    stopCurrentScan();
  }

  window.BarcodeScan = { scanOnce: scanOnce, cancel: cancel, extractGtin: extractGtin };
})();
