(() => {
  "use strict";

  const page = document.body.dataset.controlPage;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
  let selectedOrderKey = null;
  let ordersCache = [];
  let logLinesCache = [];
  let marketToggleBusy = false;
  let scheduleBaseline = null;
  let scheduleDraftDirty = false;
  let scheduleSaving = false;
  let closeActiveTimePicker = null;
  let fullCycleLaunchPending = false;
  let fullCycleRunningObserved = false;
  let fullCycleQueuedAt = 0;
  const processIcons = Object.freeze({
    marketplace_cycle: "/static/images/processes/tick.png",
    refresh_sklad: "/static/images/processes/packing.png",
    stocks: "/static/images/processes/package.png",
    orders: "/static/images/processes/tracking.png",
    prices: "/static/images/processes/price-tag.png",
    ozon_actions: "/static/images/processes/no-sales.png",
    database_backup: "/static/images/processes/data-backup.png",
  });

  function escapeHtml(value = "") {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function showToast(message, isError = false) {
    const toast = $("#control-toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("visible");
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3500);
  }

  async function requestJson(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    headers.set("X-Requested-With", "XMLHttpRequest");
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase())) {
      headers.set("X-CSRF-Token", csrfToken);
    }
    const response = await fetch(path, { ...options, headers });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = { message: await response.text() };
    }
    if (!response.ok) {
      throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    }
    return payload;
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }

  function formatDuration(value) {
    if (value === null || value === undefined) return "—";
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} сек`;
    return `${Math.floor(seconds / 60)} мин ${Math.round(seconds % 60)} сек`;
  }

  function statusText(status) {
    return {
      never: "Нет запусков",
      running: "Выполняется",
      success: "Успешно",
      partial: "Есть ошибки",
      error: "Ошибка",
      interrupted: "Прервано",
      skipped: "Пропущено",
      skipped_locked: "Уже запущено",
      disabled: "Выключено",
      processing: "В обработке",
    }[status] || status || "Ожидание";
  }

  function updateHeaderMode(enabled) {
    const status = $("#ymwb-header-status");
    const dot = $("#ymwb-header-status-dot");
    const label = $("#ymwb-header-status-label");
    if (!status || !dot || !label) return;

    const automatic = Boolean(enabled);
    label.textContent = automatic ? "Автоматический" : "Ручной";
    dot.className = `ymwb-status-dot status-${automatic ? "success" : "disabled"}`;
    status.title = automatic
      ? "Автоматический режим синхронизации"
      : "Ручной режим синхронизации";
  }

  function readProcessSchedule() {
    return {
      enabled: Boolean($("#process-schedule-enabled")?.checked),
      off_day: $("#process-schedule-off-day")?.value || "",
      off_time: $("#process-schedule-off-time")?.value || "",
      on_day: $("#process-schedule-on-day")?.value || "",
      on_time: $("#process-schedule-on-time")?.value || "",
    };
  }

  function scheduleKey(schedule) {
    return JSON.stringify({
      enabled: Boolean(schedule.enabled),
      off_day: schedule.off_day || "",
      off_time: schedule.off_time || "",
      on_day: schedule.on_day || "",
      on_time: schedule.on_time || "",
    });
  }

  function updateProcessScheduleControls() {
    const card = $("#process-market-schedule");
    if (!card) return;
    const enabled = Boolean($("#process-schedule-enabled")?.checked);
    card.classList.toggle("is-disabled", !enabled);
    $("#process-schedule-status").textContent = enabled ? "Включено" : "Выключено";
    const saveButton = $("#save-market-schedule");
    const saveDisabled = scheduleSaving || !scheduleDraftDirty;
    saveButton.disabled = saveDisabled;
    saveButton.setAttribute("aria-disabled", String(saveDisabled));
    saveButton.classList.toggle("is-saving", scheduleSaving);
    saveButton.classList.toggle("has-unsaved-changes", scheduleDraftDirty && !scheduleSaving);
    saveButton.textContent = scheduleSaving ? "Сохраняем…" : "Сохранить расписание";
  }

  function markProcessScheduleDirty() {
    scheduleDraftDirty = scheduleKey(readProcessSchedule()) !== scheduleBaseline;
    updateProcessScheduleControls();
  }

  function configureFiniteTimePicker(input) {
    const shell = input.closest(".time-picker-shell");
    const valueDisplay = shell?.querySelector(".time-picker-value");
    if (!shell || !valueDisplay) return;

    const picker = document.createElement("div");
    picker.className = "finite-time-picker";
    picker.hidden = true;
    picker.innerHTML = `
      <div class="finite-time-picker-head" aria-hidden="true"><span>Часы</span><span>Минуты</span></div>
      <div class="finite-time-picker-columns">
        <div class="finite-time-list" data-time-part="hour" role="listbox" aria-label="Часы"></div>
        <div class="finite-time-list" data-time-part="minute" role="listbox" aria-label="Минуты"></div>
      </div>`;
    document.body.appendChild(picker);

    const hourList = picker.querySelector('[data-time-part="hour"]');
    const minuteList = picker.querySelector('[data-time-part="minute"]');
    const twoDigits = value => String(value).padStart(2, "0");

    function createOptions(list, maximum) {
      for (let value = 0; value <= maximum; value += 1) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "finite-time-option";
        option.dataset.value = twoDigits(value);
        option.textContent = twoDigits(value);
        option.setAttribute("role", "option");
        list.appendChild(option);
      }
    }
    createOptions(hourList, 23);
    createOptions(minuteList, 59);

    function syncDisplayedValue() {
      valueDisplay.textContent = input.value || "—";
    }
    function selectedParts() {
      const [hour = "00", minute = "00"] = String(input.value || "00:00").split(":");
      return { hour, minute };
    }
    function selectOption(list, value) {
      list.querySelectorAll(".finite-time-option").forEach(option => {
        const selected = option.dataset.value === value;
        option.classList.toggle("is-selected", selected);
        option.setAttribute("aria-selected", String(selected));
      });
      return list.querySelector(`.finite-time-option[data-value="${value}"]`);
    }
    function positionPicker() {
      if (picker.hidden) return;
      const shellRect = shell.getBoundingClientRect();
      const pickerRect = picker.getBoundingClientRect();
      const left = Math.min(Math.max(8, shellRect.left), window.innerWidth - pickerRect.width - 8);
      let top = shellRect.bottom + 6;
      if (top + pickerRect.height > window.innerHeight - 8) top = Math.max(8, shellRect.top - pickerRect.height - 6);
      picker.style.left = `${left}px`;
      picker.style.top = `${top}px`;
    }
    function closePicker() {
      picker.hidden = true;
      shell.setAttribute("aria-expanded", "false");
      if (closeActiveTimePicker === closePicker) closeActiveTimePicker = null;
    }
    function openPicker() {
      if (!picker.hidden) {
        closePicker();
        return;
      }
      if (closeActiveTimePicker) closeActiveTimePicker();
      closeActiveTimePicker = closePicker;
      const parts = selectedParts();
      const selectedHour = selectOption(hourList, parts.hour);
      const selectedMinute = selectOption(minuteList, parts.minute);
      picker.hidden = false;
      shell.setAttribute("aria-expanded", "true");
      positionPicker();
      window.requestAnimationFrame(() => {
        selectedHour?.scrollIntoView({ block: "center" });
        selectedMinute?.scrollIntoView({ block: "center" });
      });
    }
    function applyPart(part, value) {
      const parts = selectedParts();
      parts[part] = value;
      input.value = `${parts.hour}:${parts.minute}`;
      syncDisplayedValue();
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      selectOption(part === "hour" ? hourList : minuteList, value);
    }

    hourList.addEventListener("click", event => {
      const option = event.target.closest(".finite-time-option");
      if (option) applyPart("hour", option.dataset.value);
    });
    minuteList.addEventListener("click", event => {
      const option = event.target.closest(".finite-time-option");
      if (!option) return;
      applyPart("minute", option.dataset.value);
      closePicker();
      shell.focus();
    });
    shell.addEventListener("click", event => {
      event.preventDefault();
      openPicker();
    });
    shell.addEventListener("keydown", event => {
      if (event.key === "Escape") closePicker();
      else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPicker();
      }
    });
    document.addEventListener("pointerdown", event => {
      if (!picker.hidden && !shell.contains(event.target) && !picker.contains(event.target)) closePicker();
    });
    window.addEventListener("resize", positionPicker);
    window.addEventListener("scroll", positionPicker, true);
    input.syncDisplayedValue = syncDisplayedValue;
    syncDisplayedValue();
  }
  function renderMarketSchedule(schedule, force = false) {
    const card = $("#process-market-schedule");
    if (!card) return;

    if (scheduleBaseline === null || !scheduleDraftDirty || force) {
      const fallbackDays = [
        { value: "mon", label: "Пн" }, { value: "tue", label: "Вт" },
        { value: "wed", label: "Ср" }, { value: "thu", label: "Чт" },
        { value: "fri", label: "Пт" }, { value: "sat", label: "Сб" },
        { value: "sun", label: "Вс" },
      ];
      const days = Array.isArray(schedule.days) && schedule.days.length ? schedule.days : fallbackDays;
      const options = days.map(day => `<option value="${escapeHtml(day.value)}">${escapeHtml(day.label)}</option>`).join("");
      $("#process-schedule-off-day").innerHTML = options;
      $("#process-schedule-on-day").innerHTML = options;
      $("#process-schedule-enabled").checked = Boolean(schedule.enabled);
      $("#process-schedule-off-day").value = schedule.off_day || "fri";
      $("#process-schedule-off-time").value = schedule.off_time || "18:00";
      $("#process-schedule-off-time").syncDisplayedValue?.();
      $("#process-schedule-on-day").value = schedule.on_day || "mon";
      $("#process-schedule-on-time").value = schedule.on_time || "09:00";
      $("#process-schedule-on-time").syncDisplayedValue?.();
      scheduleBaseline = scheduleKey(readProcessSchedule());
      scheduleDraftDirty = false;
    }

    if (schedule.next_event) {
      const nextAction = schedule.next_event.action === "off" ? "Выключение WB" : "Включение WB";
      const nextMoment = [schedule.next_event.day_label, schedule.next_event.time].filter(Boolean).join(" ");
      $("#schedule-message").textContent = `${nextAction}: ${nextMoment || schedule.next_event.display || schedule.next_event.at || "—"}`;
    } else {
      $("#schedule-message").textContent = schedule.enabled
        ? "Расписание включено, ближайшее событие не определено."
        : "Недельное расписание выключено.";
    }
    updateProcessScheduleControls();
  }
  function renderProcesses(payload) {
    const automation = payload.automation;
    updateHeaderMode(automation.enabled);
    $("#last-cycle").textContent = automation.last_finished_display || "—";
    $("#next-cycle").textContent = automation.next_run_display || "—";
    $("#cycle-interval").value = automation.interval_minutes;
    $("#cycle-enabled").checked = Boolean(automation.enabled);
    $("#cycle-enabled-label").textContent = automation.enabled ? "Включена" : "Выключена";
    $("#cycle-automation-control")?.classList.toggle("enabled", Boolean(automation.enabled));

    const fullCycleProcess = payload.processes.find(process => process.code === "marketplace_cycle");
    const fullCycleServerRunning = Boolean(automation.running) || fullCycleProcess?.last_status === "running";
    if (fullCycleServerRunning) {
      fullCycleRunningObserved = true;
    } else if (fullCycleLaunchPending && fullCycleRunningObserved) {
      fullCycleLaunchPending = false;
      fullCycleRunningObserved = false;
      fullCycleQueuedAt = 0;
    } else if (fullCycleLaunchPending && Date.now() - fullCycleQueuedAt > 120000) {
      fullCycleLaunchPending = false;
      fullCycleQueuedAt = 0;
    }
    const fullCycleBusy = fullCycleServerRunning || fullCycleLaunchPending;
    const fullCycleButton = $("#run-full-cycle");
    if (fullCycleButton) {
      fullCycleButton.disabled = fullCycleBusy;
      fullCycleButton.setAttribute("aria-busy", String(fullCycleBusy));
      fullCycleButton.textContent = fullCycleBusy ? "Полный цикл выполняется…" : "Запустить полный цикл";
    }
    clearTimeout(renderProcesses.fullCycleRefreshTimer);
    renderProcesses.fullCycleRefreshTimer = fullCycleBusy
      ? setTimeout(() => loadProcesses(true), 2000)
      : null;

    const grid = $("#process-grid");
    grid.innerHTML = payload.processes.map((process) => {
      const canRun = ["marketplace_cycle", "refresh_sklad", "stocks", "orders", "prices", "ozon_actions", "database_backup"].includes(process.code);
      const iconPath = processIcons[process.code] || "";
      return `
        <article class="process-card">
          <div class="process-head">
            <div class="process-summary">
              ${iconPath ? `<img class="process-icon" src="${iconPath}" alt="" aria-hidden="true">` : ""}
              <div class="process-copy">
                <h3>${escapeHtml(process.name)}</h3>
                <p>${escapeHtml(process.description)}</p>
              </div>
            </div>
            <span class="status-pill ${escapeHtml(process.last_status)}">${escapeHtml(statusText(process.last_status))}</span>
          </div>
          <div class="process-meta">
            <div><small>Последний запуск</small><strong>${escapeHtml(formatDate(process.last_started_at))}</strong></div>
            <div><small>Длительность</small><strong>${escapeHtml(formatDuration(process.last_duration_seconds))}</strong></div>
            <div><small>Последний успех</small><strong>${escapeHtml(formatDate(process.last_success_at))}</strong></div>
            <div><small>Ошибок подряд</small><strong>${Number(process.consecutive_failures || 0)}</strong></div>
          </div>
          ${canRun ? `<div class="process-actions"><button class="control-button secondary compact" data-run-process="${escapeHtml(process.code)}" ${(process.last_status === "running" || (process.code === "marketplace_cycle" && fullCycleBusy)) ? "disabled" : ""}>${process.code === "marketplace_cycle" && fullCycleBusy ? "Выполняется…" : "Запустить"}</button></div>` : ""}
        </article>`;
    }).join("");

    $("#process-runs").innerHTML = payload.runs.length ? payload.runs.map((run) => `
      <tr>
        <td><strong>${escapeHtml(run.process_name)}</strong></td>
        <td>${run.trigger === "manual" ? "Вручную" : run.trigger === "scheduler" ? "По расписанию" : escapeHtml(run.trigger)}</td>
        <td>${escapeHtml(formatDate(run.started_at))}</td>
        <td>${escapeHtml(formatDuration(run.duration_seconds))}</td>
        <td><span class="status-pill ${escapeHtml(run.status)}">${escapeHtml(statusText(run.status))}</span></td>
        <td>${escapeHtml(run.message || "—")}</td>
      </tr>`).join("") : `<tr><td colspan="6"><div class="empty-state">История появится после первого запуска.</div></td></tr>`;

    const marketNames = { yandex: "Yandex", ozon: "Ozon", wildberries: "Wildberries" };
    const marketOrder = ["wildberries", "yandex", "ozon"];
    $("#market-state-list").innerHTML = marketOrder.map((market) => {
      const enabled = Boolean(payload.market_states[market]);
      return `
      <div class="market-state ${enabled ? "enabled" : ""}">
        <span class="market-state-name"><i class="market-state-dot"></i>${escapeHtml(marketNames[market] || market)}</span>
        <div class="market-state-actions">
          <strong>${enabled ? "ON" : "OFF"}</strong>
          <label class="market-switch" title="${enabled ? "Отключить" : "Включить"} ${escapeHtml(marketNames[market] || market)}">
            <input type="checkbox" data-toggle-market="${escapeHtml(market)}" ${enabled ? "checked" : ""} ${marketToggleBusy ? "disabled" : ""}>
            <i></i>
          </label>
        </div>
      </div>`;
    }).join("");

    renderMarketSchedule(payload.market_schedule || {});
  }

  async function loadProcesses(quiet = false) {
    try {
      renderProcesses(await requestJson("/api/control/state"));
    } catch (error) {
      if (!quiet) showToast(error.message, true);
    }
  }

  async function runProcess(code, button) {
    const isFullCycle = code === "marketplace_cycle";
    let queued = false;
    button.disabled = true;
    if (isFullCycle) {
      fullCycleLaunchPending = true;
      fullCycleRunningObserved = false;
      fullCycleQueuedAt = Date.now();
      $("#run-full-cycle")?.setAttribute("aria-busy", "true");
      if ($("#run-full-cycle")) $("#run-full-cycle").textContent = "Запускаем полный цикл…";
      $$('[data-run-process="marketplace_cycle"]').forEach(cycleButton => {
        cycleButton.disabled = true;
        cycleButton.textContent = "Запускается…";
      });
    }
    try {
      const payload = await requestJson(`/api/control/processes/${encodeURIComponent(code)}/run`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      queued = true;
      showToast(payload.message || "Процесс поставлен в очередь");
      await loadProcesses(true);
    } catch (error) {
      if (isFullCycle) {
        fullCycleLaunchPending = false;
        fullCycleRunningObserved = false;
        fullCycleQueuedAt = 0;
      }
      showToast(error.message, true);
    } finally {
      if (!isFullCycle || !queued) {
        button.disabled = false;
      }
      if (isFullCycle && !queued) {
        const fullCycleButton = $("#run-full-cycle");
        if (fullCycleButton) {
          fullCycleButton.disabled = false;
          fullCycleButton.setAttribute("aria-busy", "false");
          fullCycleButton.textContent = "Запустить полный цикл";
        }
        $$('[data-run-process="marketplace_cycle"]').forEach(cycleButton => {
          cycleButton.disabled = false;
          cycleButton.textContent = "Запустить";
        });
      }
    }
  }

  function renderOrderList() {
    const list = $("#order-list");
    $("#order-count").textContent = `${ordersCache.length} шт.`;
    if (!ordersCache.length) {
      list.innerHTML = `<div class="empty-state"><strong>Заказов пока нет</strong><p>Новые заказы начнут появляться после обработки очередного цикла.</p></div>`;
      $("#order-detail").innerHTML = `<div class="empty-state"><strong>История накапливается с момента обновления</strong><p>Старый файл ID не содержит товаров и этапов обработки.</p></div>`;
      return;
    }
    list.innerHTML = ordersCache.map((order) => {
      const key = `${order.marketplace}:${order.order_id}`;
      return `
        <button class="order-list-item ${key === selectedOrderKey ? "active" : ""}"
                data-marketplace="${escapeHtml(order.marketplace)}"
                data-order-id="${escapeHtml(order.order_id)}">
          <strong>${escapeHtml(order.marketplace)} · ${escapeHtml(order.order_id)}</strong>
          <time>${escapeHtml(formatDate(order.updated_at))}</time>
          <p>${escapeHtml(order.product_summary || order.last_message || "Состав заказа уточняется")}</p>
          <span class="status-pill ${escapeHtml(order.status)}">${escapeHtml(statusText(order.status))}</span>
        </button>`;
    }).join("");
  }

  function renderOrderDetail(order) {
    const steps = order.steps || [];
    const productTitle = order.product_summary || order.last_message || "Товар в заказе";
    $("#order-detail").innerHTML = `
      <div class="order-detail-head">
        <div>
          <h2>${escapeHtml(productTitle)}</h2>
          <p class="order-detail-reference">${escapeHtml(order.marketplace)}: ${escapeHtml(order.order_id)}</p>
        </div>
        <div class="order-detail-actions">
          <span class="status-pill ${escapeHtml(order.status)}">${escapeHtml(statusText(order.status))}</span>
          <button class="control-button danger compact" type="button" data-delete-order data-marketplace="${escapeHtml(order.marketplace)}" data-order-id="${escapeHtml(order.order_id)}">Удалить из базы</button>
        </div>
      </div>
      <div class="order-summary-grid">
        <div><small>Обнаружен</small><strong>${escapeHtml(formatDate(order.detected_at))}</strong></div>
        <div><small>Поставщик</small><strong>${escapeHtml(order.supplier || "—")}</strong></div>
        <div><small>Сумма</small><strong>${order.total_amount == null ? "—" : `${Number(order.total_amount).toLocaleString("ru-RU")} ₽`}</strong></div>
      </div>
      <p class="control-eyebrow">Этапы обработки</p>
      <div class="steps-list">
        ${steps.length ? steps.map((step) => `
          <div class="order-step ${escapeHtml(step.status)}">
            <i class="step-dot"></i>
            <div><strong>${escapeHtml(step.step_name)}</strong><small>${escapeHtml(step.message || statusText(step.status))}</small></div>
            <time>${escapeHtml(formatDate(step.updated_at))}</time>
          </div>`).join("") : `<div class="empty-state">Этапы ещё не записаны.</div>`}
      </div>`;
  }

  async function loadOrders({ selectFirst = false } = {}) {
    const query = new URLSearchParams({
      query: $("#order-query")?.value || "",
      marketplace: $("#order-marketplace")?.value || "",
      status: $("#order-status")?.value || "",
      limit: "150",
    });
    try {
      const payload = await requestJson(`/api/control/orders?${query}`);
      ordersCache = payload.orders || [];
      if (selectFirst && ordersCache.length) {
        selectedOrderKey = `${ordersCache[0].marketplace}:${ordersCache[0].order_id}`;
      }
      renderOrderList();
      if (selectedOrderKey) {
        const [marketplace, ...orderParts] = selectedOrderKey.split(":");
        await loadOrderDetail(marketplace, orderParts.join(":"));
      }
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function loadOrderDetail(marketplace, orderId) {
    try {
      const payload = await requestJson(`/api/control/orders/${encodeURIComponent(marketplace)}/${encodeURIComponent(orderId)}`);
      selectedOrderKey = `${marketplace}:${orderId}`;
      renderOrderList();
      renderOrderDetail(payload.order);
    } catch (error) {
      showToast(error.message, true);
    }
  }
  async function deleteOrder(marketplace, orderId, button) {
    const confirmed = window.confirm(
      `Удалить заказ ${orderId} и разрешить его повторную обработку?\n\n` +
      "Карточка, этапы и отметка «уже обработан» будут удалены. При следующем цикле остаток спишется, запись в Google Sheets и уведомления будут созданы повторно."
    );
    if (!confirmed) return;

    button.disabled = true;
    try {
      const payload = await requestJson(
        `/api/control/orders/${encodeURIComponent(marketplace)}/${encodeURIComponent(orderId)}`,
        { method: "DELETE" }
      );
      showToast(payload.message || "Заказ удалён; повторная обработка разрешена");
      selectedOrderKey = null;
      await loadOrders({ selectFirst: true });
    } catch (error) {
      showToast(error.message, true);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  }

  function renderLogs() {
    const level = $("#log-level")?.value || "";
    const search = ($("#log-search")?.value || "").trim().toLowerCase();
    const filtered = logLinesCache.filter((line) => {
      if (level && !line.includes(`| ${level}`) && !line.includes(`| ${level.padEnd(8)}`)) return false;
      return !search || line.toLowerCase().includes(search);
    });
    $("#log-viewer").innerHTML = filtered.length ? filtered.map((line) => {
      const found = line.match(/\|\s*(DEBUG|INFO|SUCCESS|WARNING|ERROR)\s*\|/);
      return `<span class="log-line ${found ? found[1] : ""}">${escapeHtml(line)}</span>`;
    }).join("") : `<span class="log-line">Совпадений не найдено.</span>`;
  }

  function renderOperatorActions(actions) {
    $("#operator-actions").innerHTML = actions.length ? actions.map((action) => `
      <tr>
        <td>${escapeHtml(formatDate(action.created_at))}</td>
        <td>${escapeHtml(action.actor)}</td>
        <td>${escapeHtml(action.action)}</td>
        <td>${escapeHtml(action.summary)}</td>
      </tr>`).join("") : `<tr><td colspan="4"><div class="empty-state">Действия появятся после изменения настроек или ручных запусков.</div></td></tr>`;
  }

  async function loadLogs() {
    try {
      const lines = $("#log-lines")?.value || "150";
      const payload = await requestJson(`/api/control/logs?lines=${encodeURIComponent(lines)}`);
      logLinesCache = payload.lines || [];
      renderLogs();
      renderOperatorActions(payload.actions || []);
      return true;
    } catch (error) {
      showToast(error.message, true);
      return false;
    }
  }

  if (page === "processes") {
    $$(".time-picker-storage", $("#process-market-schedule")).forEach(configureFiniteTimePicker);
    loadProcesses();
    $("#run-full-cycle")?.addEventListener("click", (event) => runProcess("marketplace_cycle", event.currentTarget));
    $("#process-grid")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-run-process]");
      if (button) runProcess(button.dataset.runProcess, button);
    });
    $("#save-cycle-interval")?.addEventListener("click", async () => {
      try {
        await requestJson("/automation/interval", {
          method: "POST",
          body: JSON.stringify({ interval_minutes: Number($("#cycle-interval").value) }),
        });
        showToast("Интервал сохранён");
        await loadProcesses(true);
      } catch (error) {
        showToast(error.message, true);
      }
    });
    $("#market-state-list")?.addEventListener("change", async (event) => {
      const input = event.target.closest("[data-toggle-market]");
      if (!input) return;
      if (marketToggleBusy) {
        input.checked = !input.checked;
        return;
      }

      const requestedState = input.checked;
      const market = input.dataset.toggleMarket;
      marketToggleBusy = true;
      $$("[data-toggle-market]").forEach(item => { item.disabled = true; });
      try {
        const result = await requestJson(`/toggle_stock/${encodeURIComponent(market)}`, {method: "POST"});
        input.checked = Boolean(result.enabled);
        showToast(`${market} ${result.enabled ? "включён" : "выключен"}`);
      } catch (error) {
        input.checked = !requestedState;
        showToast(error.message, true);
      } finally {
        marketToggleBusy = false;
        await loadProcesses(true);
      }
    });
    $("#cycle-enabled")?.addEventListener("change", async (event) => {
      try {
        const status = await requestJson("/automation/toggle", {
          method: "POST",
          body: JSON.stringify({ enabled: event.target.checked }),
        });
        event.target.checked = Boolean(status.enabled);
        updateHeaderMode(status.enabled);
        showToast(status.enabled ? "Автосинхронизация включена" : "Автосинхронизация приостановлена");
        await loadProcesses(true);
      } catch (error) {
        event.target.checked = !event.target.checked;
        showToast(error.message, true);
      }
    });
    $("#process-market-schedule")?.addEventListener("input", markProcessScheduleDirty);
    $("#process-market-schedule")?.addEventListener("change", markProcessScheduleDirty);
    $("#save-market-schedule")?.addEventListener("click", async () => {
      scheduleSaving = true;
      updateProcessScheduleControls();
      try {
        const result = await requestJson("/market-schedule", {
          method: "POST",
          body: JSON.stringify(readProcessSchedule()),
        });
        renderMarketSchedule(result, true);
        showToast("Недельное расписание WB сохранено");
      } catch (error) {
        showToast(error.message, true);
      } finally {
        scheduleSaving = false;
        updateProcessScheduleControls();
      }
    });
    setInterval(() => loadProcesses(true), 10000);
  }

  if (page === "orders") {
    loadOrders({ selectFirst: true });
    $("#refresh-orders")?.addEventListener("click", () => loadOrders());
    $("#order-filters")?.addEventListener("submit", (event) => {
      event.preventDefault();
      selectedOrderKey = null;
      loadOrders({ selectFirst: true });
    });
    $("#order-list")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-order-id]");
      if (button) loadOrderDetail(button.dataset.marketplace, button.dataset.orderId);
    });
    $("#order-detail")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-delete-order]");
      if (button) deleteOrder(button.dataset.marketplace, button.dataset.orderId, button);
    });
  }

  if (page === "logs") {
    loadLogs();
    $("#refresh-logs")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "Обновляем…";
      const updated = await loadLogs();
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
      button.textContent = "Обновить";
      if (updated) showToast("Журнал обновлён");
    });
    $("#download-log")?.addEventListener("click", (event) => {
      const link = event.currentTarget;
      if (link.classList.contains("is-processing")) {
        event.preventDefault();
        return;
      }
      link.classList.add("is-pressed", "is-processing");
      link.setAttribute("aria-busy", "true");
      link.textContent = "Скачиваем…";
      showToast("Скачивание журнала началось");
      setTimeout(() => {
        link.classList.remove("is-pressed", "is-processing");
        link.setAttribute("aria-busy", "false");
        link.textContent = "Скачать лог";
      }, 900);
    });
    $("#log-level")?.addEventListener("change", renderLogs);
    $("#log-search")?.addEventListener("input", renderLogs);
    $("#log-lines")?.addEventListener("change", loadLogs);
    $(".monitor-tabs")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-monitor-target]");
      if (!button) return;
      $$(".monitor-tabs button").forEach((item) => item.classList.toggle("active", item === button));
      $$(".monitor-pane").forEach((pane) => pane.classList.toggle("active", pane.id === button.dataset.monitorTarget));
    });
  }
})();
