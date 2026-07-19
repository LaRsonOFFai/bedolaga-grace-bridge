# Установка

## Требования

- Ubuntu/Debian Linux;
- Docker и Docker Compose v2;
- точный upstream commit Bedolaga v3.64.0 из матрицы совместимости;
- PostgreSQL;
- действующий Remnawave API key;
- заранее созданный Grace internal squad;
- минимум 2 GiB свободного места для дампа и образов.

## Этапы

1. Скачать `install.sh` и `SHA256SUMS` из GitHub Release.
2. Проверить SHA-256 и просмотреть скрипт.
3. Запустить `sudo bash install.sh`.
4. Мастер сохранит секреты в `secrets.env` с правами `0600`.
5. `preflight` проверит commit и затрагиваемые файлы.
6. `sudo gracectl install` создаст дамп и выключенный кандидат.
7. `sudo gracectl observe` покажет кандидатов без записей.
8. `sudo gracectl canary` запросит один UUID скрытым от shell history.
9. После проверки оплаты выполнить `sudo gracectl approve-canary`.
10. Расширять только командами `sudo gracectl activate`: 5%, 25%, 50%, 100%.

Ни `install.sh`, ни `gracectl install` не активируют Grace для клиентов.

## Пользовательская правка auth

Отдельная правка, позволяющая кабинету принимать старый токен, сохранится, если
её файл не входит в патч Grace. Если код Bedolaga изменён в одном из
затрагиваемых файлов, установка остановится, а не перезапишет правку.
