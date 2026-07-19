# Bedolaga Grace Bridge

Безопасный мост между **Bedolaga** и **Remnawave**, который выдаёт ранее
платившим пользователям ограниченный Grace-доступ после окончания подписки и
без гонок возвращает обычный доступ после оплаты.

> Первый compatibility package рассчитан только на точный upstream commit
> **Bedolaga v3.64.0**. Более старые, более новые и локально изменённые
> затрагиваемые файлы блокируются до любых изменений.

## Главный принцип

Установка никогда не включает Grace для всего пула клиентов. Переходы разделены:

```text
preflight -> backup -> install(disabled) -> observe -> canary -> activate
```

- неизвестная версия Bedolaga — остановка без изменений;
- несовпавшая контрольная сумма — остановка без изменений;
- перед первой записью создаётся резервная копия;
- `install` оставляет интеграцию выключенной;
- `canary` допускает только один явно выбранный UUID;
- `activate` требует отдельную фразу подтверждения;
- аварийный `gracebridge-rescue` не зависит от контейнера Bridge.

## Быстрый безопасный старт

После публикации первого релиза:

```bash
curl -fsSLO https://github.com/LaRsonOFFai/bedolaga-grace-bridge/releases/download/v0.1.0/install.sh
curl -fsSLO https://github.com/LaRsonOFFai/bedolaga-grace-bridge/releases/download/v0.1.0/SHA256SUMS
grep ' install.sh$' SHA256SUMS | sha256sum -c -
less install.sh
sudo bash install.sh
```

Установщик сначала запускает только read-only проверку. Для управления:

```bash
sudo gracectl preflight
sudo gracectl status
sudo gracectl observe
sudo gracectl canary
sudo gracectl activate
sudo gracectl pause
sudo gracectl resume
sudo gracectl rollback
sudo gracebridge-rescue
```

Подробности: [архитектура](docs/architecture.md),
[установка](docs/installation.md), [откат](docs/recovery.md) и
[матрица совместимости](docs/compatibility.md). Результаты изолированного
теста на 40 000 записей: [нагрузочное тестирование](docs/load-testing.md).

## Что Bridge не делает

Bridge назначает заранее подготовленный internal squad и лимит трафика. Он не
создаёт универсальную сетевую политику Telegram: маршрутизация Telegram, Mini
App, DNS, оплаты и ограничение скорости должны быть настроены на Grace-ноде.

## Масштабирование

Кандидаты читаются keyset-пагинацией, команды исполняются ограниченным пулом,
а изменения одного пользователя защищены PostgreSQL advisory lock и номером
поколения. Детерминированный rollout на 40 000 UUID входит в быстрые
регрессионные тесты; отдельный PostgreSQL-нагрузочный тест запускается только в
изолированной базе и никогда не обращается к рабочей Remnawave.

## Статус и происхождение

Это независимый проект, не официальный компонент Bedolaga или Remnawave.
Исходный код `zavul0nn/remnawave-grace-access` не копировался. Подробнее —
[NOTICE](NOTICE).
