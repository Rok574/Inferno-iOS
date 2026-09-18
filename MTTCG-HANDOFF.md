# Хендофф: MTTCG-зависание восстановленной iOS 16.3.1 после Setup

Самодостаточная передача дел. Читается «с холода». Цель — **iPhone** (эмулятор в
софте, TCG/MTTCG; виртуализации/HVF на iPhone нет — не отвлекаться на HVF). Mac —
только dev-стенд для быстрого цикла.

## 1. Что уже работает (НЕ ломать)

- **Рестор iOS 16.3.1 (20D67) грузится до Setup Assistant** («Hello»/«olá») на
  Mac-стенде под MTTCG. Это фронтир публичного форка ChefKiss (рестор 16 публично
  не делали, #305). Полный разбор рестора — в `RESTORE-HANDOFF.md`.
- **Рабочие патчи ядра в эмуляторе** (`inferno-src/hw/arm/kernel_patches.c`, ветка
  `ios`) — ОСТАВИТЬ:
  - `allow rooting the live fs of a sealed volume` — нопит `tbnz w8,#5` на бите
    «том запечатан» в `apfs_vfsop_mount`; без него запечатанный рестором том не
    грузится (паника apfs_vfsops.c:2412).
  - `bypass pmap_cs_enforce` — переписан на callback (якорь `mov x8,#-0x31d`),
    иначе не применялся на 16.3.1.
- **single-thread (`THREAD=single`) грузится ДАЛЬШЕ и стабильнее** — проходит
  мёртвую точку MTTCG. Это ключевая улика: баг **MTTCG-специфичный** (межпоточный).

## 2. Открытый баг

Под **MTTCG** восстановленная 16.3.1 доходит до Setup, ~минуту живёт (нормальный
FPS), затем **зависает — потерянное пробуждение (lost wakeup)** заблокированного
потока. Проявляется в двух режимах (недетерминированно, зависит от таймингов):

- **busy-spin** (чаще, ближе к Setup): 3 ядра в полке (cpu ~300%), спинят в
  **userspace (EL0)**, часто 2 ядра в одном регионе (лок в dyld shared cache);
  4-е ядро запарковано; лог/часы стоят. Держатель `os_unfair_lock` заблокирован и
  не просыпается → спиннеры крутятся вечно.
- **idle-WFI** (реже, ранний boot, напр. `mount-phase-2`): ВСЕ ядра спят в WFI
  (`0xfffffff007d517f0` = `wfi;cbz x30;ret` цикл парковки), cpu ~7%, разбудить
  некому.

Обе — одна причина: некий поток ждёт пробуждения (reschedule-IPI/таймер), событие
теряется под MTTCG, поток не встаёт. При многих готовых потоках → busy-spin на его
локе; при малом числе → все уходят в WFI.

## 3. Воспроизведение (Mac-стенд)

```bash
# 1) откатить состояние на чистый пост-рестор снимок (склонирован cp -c)
cd ~/inferno-ios/ios1631/InfernoData
for f in root sep_nvram sep_ssc nvram effaceable ctrl_bits syscfg firmware panic_log; do
  rm -f "$f"; cp -c "$f.restored" "$f"; done
# 2) muxd (USB-хост) должен слушать
pgrep -f muxd.py || (cd "/Users/makr/Documents/Inferno Pixel7" && \
  python3 netlab/muxd.py --usb /tmp/iusb.sock --socket /tmp/inferno-usbmuxd &)
# 3) загрузка под MTTCG (виснет через ~минуту после Setup, недетерминированно)
cd "/Users/makr/Documents/Inferno Pixel7"
DATA=~/inferno-ios/ios1631 GUI=none ACCEL=tcg MEM=4G netlab/lab16.sh boot
#   THREAD=single … — НЕ виснет (контроль-эксперимент)
#   GUI=sdl — окно; ACCEL=hvf — НЕ для цели (iPhone), падает на GENTER
```
- Консоль гостя: `~/inferno-ios/ios1631/guest-boot.log`. Лог эмулятора:
  `qemu-boot.log`. QMP: `/tmp/inf-lab16.qmp`. Загрузка до Setup под MTTCG ~15–45 мин.
- Кернелкэш для дизасма: `~/inferno-ios/ios1631/analysis/kc.raw` (символы только
  kext'ов; XNU-ядро НЕ символизируется — nm не поможет).
- Детектор зависания и захват состояния: `/tmp/kwork/aichunt.sh` (fb-static+лог
  стоит → снимает PC ядер + AICDIAG). Кадр: QMP `screendump`.

## 4. Диагностика: что ИСКЛЮЧЕНО (не повторять)

- **НЕ порча тома DATA / не мои обрывы**: виснет на чистом пристина-состоянии,
  `storm=0` (шторм `reset ino` не при чём).
- **НЕ unimplemented sysreg**: в qemu-логе чисто.
- **НЕ WFE**: у форка WFE = NOP (для корректности безвреден, спин а не тупик).
- **НЕ AIC**: за 4299 сэмплов 1Гц-дампа ВСЕ значения константны —
  `ipi_mask=0x80000001` (reset-дефолт), `tmr_cfg=0`, `armed=0`, pending/deferred
  IPI = 0. **iOS вообще не использует AIC для IPI и per-CPU таймера** — только для
  device-прерываний (EIR). Значит IPI и таймер идут мимо AIC.
- **НЕ `IPI_CR=0` timer_del** в `a13.c`: я думал, что запись `IPI_CR` с нулевой
  отсрочкой убивает `ipicr_timer` (доставку deferred-IPI). **Проверено: гость
  никогда не пишет IPI_CR=0** (нет событий `A13IPI write_cr`), тик жив всё время
  (`ipi_cr=64000`). Мой «фикс» этого — ИНЕРТЕН, ничего не изменил.
- **НЕ простая гонка данных в fast-IPI**: все IPI-сисреги в `a13.c`
  (IPI_RR_LOCAL/GLOBAL, IPI_SR, IPI_CR) объявлены с `.type = ARM_CP_IO` →
  выполняются под BQL, сериализованы с `ipicr_tick` (iothread, тоже BQL).
- **НЕ `apple_a13_is_off`**: корректен (`power_state == PSCI_OFF` — только PSCI
  CPU_OFF, не путает с WFI/idle). Deferred-IPI к halted-но-on ядру доставляется.

## 5. Где искать дальше (актуальные подозреваемые)

**Механизм пробуждения — `inferno-src/hw/arm/a13.c` (fast-IPI) + ARM generic timer.**
iOS будит ядра через Apple fast-IPI (сисреги `s3_5_c15_c0_x`, обработка в a13.c:
`apple_a13_ipi_rr_local/global` → `apple_a13_deliver_ipi` → `ipi_sr`+`qemu_irq_raise
(fast_ipi)`), плюс отложенная доставка через `apple_a13_cluster_ipicr_tick`
(глобальный `ipicr_timer`, период `ipi_cr=64µs`). Ack — `apple_a13_ipi_write_sr`
(`ipi_sr=0`, `qemu_irq_lower(fast_ipi)`, чистит `deferredIPI[]`).

Нерасследованные зацепки:
1. **`apple_a13_deliver_ipi`: `if (cpu->ipi_sr) { return; }`** — дропает IPI, если у
   цели уже стоит `ipi_sr`. Плюс `apple_a13_cluster_tick` отдаёт лишь ОДНО
   назначение (`ctz32`) на src за тик, а бит `deferredIPI` чистится только на ACK.
   Проверить, не теряется/не стар­веется ли пробуждение при коалесинге под MTTCG,
   даже несмотря на BQL (гонка raise-vs-WFI-enter на уровне QEMU-ядра).
2. **ARM generic timer (CNTV)**: заблокированный поток может ждать таймерный
   дедлайн, а не IPI. Проверить доставку `cpu->gt_timer_outputs[GTIMER_*]` на
   t8030 (разводка в `hw/arm/t8030.c`) и пробуждение WFI-ядра таймером под MTTCG.
3. **WFI-wake на уровне QEMU-ядра**: `qemu_irq_raise(fast_ipi)` →
   `cpu_interrupt(HARD)` → `qemu_cpu_kick`. Проверить, не теряется ли kick, если
   raise приходит ровно в момент входа vCPU в WFI (`cpu->halted`). Регистр
   `s3_5_c15_c5_0` (ARM64_REG_CPU_OVRD, бит `0x2000000`) пишется ядром прямо перед
   WFI — эмулятор его игнорирует (plain RW); проверить, не влияет ли.

**Надёжный (но долгий) метод:** инструментировать per-CPU каждое пробуждение
(fast-IPI deliver/drop, ARM-timer IRQ) и вход/выход WFI, воспроизвести, и по
застрявшему ядру определить: пробуждение к нему НЕ пришло (проблема на стороне
отправителя/планировщика) или пришло, но не разбудило (WFI-wake на уровне QEMU).

**Быстрый метод (рекомендую):** у **theGlym** (форк-от-нашего-форка, «Inferno 26»)
MTTCG уже работает — iOS 26.5 живёт под софтом на его устройстве. Его `hw/arm/a13.c`
(или диф по fast-IPI / WFI-wake / таймеру) почти наверняка содержит этот фикс. Один
взгляд на его правку экономит часы слепых итераций.

## 6. Текущие правки в форке (`inferno-src`, ветка `ios`) — что оставить/убрать

`git diff --stat`: `a13.c`, `kernel_patches.c`, `apple_aic.c`, `hvf/hvf.c`.
- **`kernel_patches.c` — ОСТАВИТЬ** (боевые патчи sealed-volume + pmap_cs, см. §1).
- **`a13.c` — УБРАТЬ/пересмотреть**: инертный «фикс» IPI_CR (не удалять таймер) +
  `fprintf` heartbeat `A13IPI tick alive` / `A13IPI write_cr`. Фикс инертен (баг
  не тут), но лог полезен для дальнейшей диагностики. Решить по ходу.
- **`apple_aic.c` — УБРАТЬ**: диагностический 1Гц `fprintf(AICDIAG …)` в
  `apple_aic_tick`. AIC оказался не при чём; лог можно снять.
- **`hvf/hvf.c` — УБРАТЬ**: `HVFDIAG` (`warn_report` в sysreg-путях). HVF не цель
  (iPhone виртуализацию не даёт). Это был отдельный тупик; см. §7.

Все `fprintf`/`warn_report` с префиксами `AICDIAG`/`A13IPI`/`HVFDIAG` — временные,
убрать перед любым коммитом. Боевой код — только `kernel_patches.c`.

## 7. Побочный тупик (для контекста, НЕ приоритет)

HVF на Mac для 16 падает на `GENTER` (GXF): различие 14-vs-16 — 16 пишет
`s3_6_c15_c3_3=0x40000` (новый Apple GXF/SPRR-регистр, 14 не трогает), под HVF он
трапится и проглатывается. Подробности в `RESTORE-HANDOFF.md`. **Для цели (iPhone)
HVF нерелевантен** — там всегда TCG/MTTCG. Не тратить время.

## 8. Полезные факты про стенд

- Один qemu за раз (порт консоли 4555, QMP-сокет). muxd.py нужен даже для `boot`.
- `inject-nmi` на t8030 НЕ поддерживается («machine does not provide NMIs») —
  форс-стек-шот так не снять. `x-query-interrupt-controllers` пуст.
- Кадр: QMP `screendump` → PPM (828×1792 = родное n104); `sips` в PNG.
- PC ядер: QMP `human-monitor-command` `info registers -a`. userspace PC = EL0-спин;
  `0xfffffff007d517f0` = WFI-idle; `0xffffffff0000bf24` (unmapped) = запаркованное.
- Дизасм kc.raw: capstone, VA→off по сегментам (kernel slide=0; __TEXT_EXEC
  vm=0xfffffff007c88000 foff=0xc84000). Скрипты — в истории сессии/`/tmp/kwork`.
