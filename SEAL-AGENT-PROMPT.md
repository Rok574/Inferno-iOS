# Задача для агента: пробить `seal_system_volume` (msys) в ресторе iOS 16.3.1

Ты продолжаешь работу на маке (M1, 8 ГБ) над восстановлением iOS **16.3.1
(20D67)** в эмуляторе iPhone **Inferno** (форк QEMU от ChefKiss). Рестор доведён
до **последнего шага** и упирается ровно в одну стену — запечатывание системного
тома. Твоя цель: пройти этот шаг так, чтобы рестор завершился и восстановленная
система загрузилась. Общайся с пользователем по-русски; код/комментарии — по-английски.

## Сначала прочитай (не переоткрывай уже сделанное)
- `RESTORE-HANDOFF.md` (корень проекта) — полная передача дел, раздел «Живой
  рестор 16.3.1 на маке» и подразделы про seal. Все адреса и логи там.
- Память проекта: `inferno-ios16-seal-blocker`, `inferno-cryptex1-signing`,
  `inferno-newer-ios-upstream`, `inferno-ios16-phone-restore-plan`.
- НЕ переизобретай: весь путь до seal уже работает (см. ниже).

## Что уже работает (пять блокеров сняты, не трогай)
1. Модель: гость `n104dev` → патч `idevicerestore` (`tools/idevicerestore/src/restore.c`,
   `restore_get_irecv_device`: суффикс `dev`→`ap`), пересобран `make`.
2. `NORData`: `netlab/muxd.py` слал 48 КБ, буфер гостя 16 КБ → `USB_MTU=16384`.
3. Обрыв образа на 96 %: `muxd.py` по закрытию клиента дренирует очередь, ждёт
   `tx_seq==tx_acked`, шлёт FIN, потом закрывает.
4. Тикет **Cryptex1 (#305)**: свой сервер подписи `netlab/tssd.py`
   (`idevicerestore --server http://127.0.0.1:8888`), дайджесты из манифеста.
5. Дайджест `msys` в AP-тикете: `tools/create_apticket.py` — fallback ключа на
   префикс `Ap,` (`Ap,SystemVolumeCanonicalMetadata`).

Записываются весь образ ОС + оба криптекса (SystemOS/AppOS) + iBoot. Всё это
проверено вживую.

## Стена: `seal_system_volume` → `msys`
На последнем шаге `restored_external` в RAM-диске генерит live-mtree тома
(`/mnt5/mtree_remap.xml`), оборачивает в Image4 и аутентифицирует компонент
**`msys`** (`Ap,SystemVolumeCanonicalMetadata`) через `img4_firmware_execute`.
Оценка даёт `dr = -1, ct = 0xaaaaaaaa, error = -1` («unknown» — это ошибка оценки,
не вердикт «не доверяю»), затем `img4_firmware_execute failed: 80` →
`CHECKPOINT FAILURE:(FAILURE:6) seal_system_volume`.

**Проверка идёт в USERSPACE** — в `usr/lib/libimg4.dylib` самого RAM-диска, НЕ в
ядре. Патчи ядра эмулятора на seal не влияют (проверено). `msys` — единственная
настоящая trust-eval за весь рестор (`grep 'authenticating firmware on chip'` даёт
только её); подпись нашего тикета до seal вообще не проверяется.

Разбор libimg4 (символы есть, `nm -n libimg4.dylib`):
- `_img4_firmware_execute`@`0xa2f4` → на `0xa338` зовёт `_img4_firmware_evaluate`@`0xa4fc`,
  на `0xa33c cbz w0,#0xa348` уходит в ошибку при ненулевом вердикте.
- внутренний evaluator `__…WithCallbacksInternal`@`0x1dea4`: успех на `0x1e120 mov w0,#0`,
  провал — `cbnz w0,#0x1e124` (их 11) и `cbz/b.hi`→`0x1e14c`/`0x1e154`. Паттерн
  окружения `orr x1,x9,#0xe000000000000000` на `0x1e048`.
- `…Internal` задевает ТОЛЬКО `msys` (пробой её по стадии рестор не ломает).

## Что уже пробовали и почему НЕ работает (не повторяй)
Байт-патчи RAM-дискового `libimg4` — RAM-диск эмулятор берёт как raw `-initrd` без
проверки хэша (override `INITRD_FILE` в `netlab/lab16.sh`); правка по файловому
смещению: `hdiutil attach -imagekey diskimage-class=CRawDiskImage -blocksize 4096
-owners on -nomount ramdisk.raw` → `diskutil mount` RW → правка → sync/unmount/detach.
Пять стратегий, ВСЕ откачены:
1. `execute 0xa33c cbz→b` — ломает фазу ДО seal (`-256`, гость в бут-цикл).
2. `evaluate 0xabd4 mov x0,x21→mov x0,#0` (единств. `retab`@`0xabf4`, x21 — код
   возврата) — тоже ломает раньше seal (restored сам `/sbin/reboot`).
3. `…Internal 0x1dea4 → mov w0,#0; ret` — SIGBUS (не заполнен выход решения).
4. `…Internal` все 11 `cbnz…0x1e124`→NOP + `0x1e068 cbz→b` — SIGBUS раньше (ранний
   колбэк оставляет мусор).
5. dr-load в ядре — не тот бинарь.

**Вывод:** байт-патчами `libimg4` seal не берётся. Провальный колбэк окружения img4
— это код **`restored_external`** (в RAM-диске, `usr/local/bin/restored_external`),
а не libimg4; без его настоящего вывода `msys` не подделать патчем библиотеки.

## Твои пути (по приоритету)
1. **Live-отладка в госте (начни с этого).** Понять, какой именно env-колбэк даёт
   ненулевое на `msys` и почему (`dr=-1`/`error=-1` — это ошибка, не «untrusted»,
   значит скорее структура/окружение, чем подпись). Варианты: поднять
   debugserver/lldb в RAM-диске (usbmux у нас есть — `muxd.py`), или добавить в
   `libimg4`/`restored_external` точечные логи-инструкции (у нас есть механизм
   правки RAM-диска байтами; можно вставить `bl` в свободное место с записью
   в консоль). Гость пишет консоль на TCP 4555 (см. `lab16.sh`).
2. **Патчить `restored_external`** — когда колбэк локализован, заставить его отдать
   «доверено» с КОРРЕКТНЫМ выводом (не короткое замыкание — от него SIGBUS).
   Бинарь без символов, но `strings`/`nm`/xref по строкам помогут.
3. Гипотезы, ещё не проверенные до конца: (а) live-mtree тома в эмуляторе не
   совпадает с каноническим msys из манифеста (тогда чинить генерацию/укладку
   APFS, а не оценку); (б) `im4m is NULL. Assuming payload with attached manifest`
   — restored не нашёл ожидаемый отдельный im4m для msys; понять, откуда он должен
   его брать и что мы недодаём в `SystemImageCanonicalMetadata`.
   Boot-time проверку root hash ядро эмулятора уже снимает
   (`bypass root hash authentication`), так что если пройти restore-time оценку —
   система должна грузиться.

## Как гонять стенд (точные команды)
Комплект: `~/inferno-ios/ios1631` (диски `InfernoData/`, тикеты, SEP, ключи 20D67,
`cryptex_template.im4m`, сам IPSW, `analysis/ramdisk.raw`, `runs/` с логами).
Эмулятор для стенда: `inferno-src/build-macos/qemu-system-aarch64` (форк на ветке
`ios`; правка эмулятора — `ninja -C build-macos qemu-system-aarch64`, но seal в
userspace, ядро тут ни при чём). HVF 16.x паникует GENTER — только MTTCG.

Один прогон (из корня проекта):
```
# 1) сброс изменяемых дисков гостя
cd ~/inferno-ios/ios1631/InfernoData && python3 -c "
for n,s in [('root',34359738368),('firmware',8<<20),('syscfg',128<<10),('ctrl_bits',8<<10),('nvram',8<<10),('effaceable',4<<10),('panic_log',1<<20),('sep_nvram',64<<10),('sep_ssc',128<<10)]:
    open(n,'wb').truncate(s)"
# 2) muxd + tssd (фон), затем эмулятор (перезапускай при wdog-панике 'Failed to mount root')
cd "/Users/makr/Documents/Inferno Pixel7"
python3 netlab/muxd.py --usb /tmp/iusb.sock --socket /tmp/inferno-usbmuxd &
~/inferno-ios/tools/venv/bin/python netlab/tssd.py --manifest ~/inferno-ios/ios1631/InfernoData/Restore/BuildManifest.plist --template ~/inferno-ios/ios1631/cryptex_template.im4m --port 8888 &
INITRD_FILE=~/inferno-ios/ios1631/analysis/ramdisk.raw GUI='sdl,show-cursor=on' ACCEL=tcg TB=256 MEM=4G DATA=~/inferno-ios/ios1631 GLOG=~/inferno-ios/ios1631/runs/restore-guest.log netlab/lab16.sh restore
# ждать 'waiting for host' в restore-guest.log и 'device 05ac' в muxd, затем:
cd ~/inferno-ios/ios1631 && USBMUXD_SOCKET_ADDRESS=UNIX:/tmp/inferno-usbmuxd \
  ~/inferno-ios/tools/idevicerestore/src/idevicerestore --erase --restore-mode -d \
  -i 0x1122334455667788 -T InfernoData/root_ticket.der --server http://127.0.0.1:8888 \
  iPhone12,1_16.3.1_20D67_Restore.ipsw > runs/restore-idr.log 2>&1
```
До seal ~8 мин (образ+криптексы под MTTCG). Выключать эмулятор — QMP `quit` на
`/tmp/inf-lab16.qmp`.

## Грабли
- Эмулятор иногда падает на старте `Failed to mount root device` (wdog) — просто
  перезапусти, заводится со 2–3 раза.
- `-256 (Could not read data)` у idevicerestore = гость перезагрузился/крашнулся,
  USB отвалился (это симптом, не причина).
- Прогресс-бары в `restore-idr.log` через `\r` — читай `tr '\r' '\n'`.
- Правишь RAM-диск — только `analysis/ramdisk.raw` (наша копия), не `.dmg`; после
  правки obязательно sync + unmount + detach.
- Не трогай форк эмулятора зря: `git -C inferno-src diff hw/arm/kernel_patches.c`
  должен быть пуст (ядро к seal отношения не имеет).

Пиши всё, что делаешь, в `RESTORE-HANDOFF.md` и память — агент может смениться.
