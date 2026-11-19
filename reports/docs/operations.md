# Робота з reports.vilnaos.com

Ключ адміністратора: `/etc/vilna-reports/admin_key` на сервері (і в менеджері паролів). Без нього `/admin` відповідає `404`.

```sh
K=$(cat ~/.vilna-reports-admin-key)        # або вставити з менеджера паролів
R=https://reports.vilnaos.com
```

## Подивитися

- У браузері: `https://reports.vilnaos.com/admin` (ім'я будь-яке, пароль — ключ): таблиця збоїв і останні відгуки.
- Збої: `curl -H "authorization: Bearer $K" $R/admin/v1/signatures`
- Один збій і до 20 останніх звітів: `curl -H "authorization: Bearer $K" $R/admin/v1/signatures/<сигнатура>`
- Відгуки разом із поштою для відповіді: `curl -H "authorization: Bearer $K" $R/admin/v1/feedback`

## Схвалити збій — відкрити issue `auto-report`

Спочатку прочитати звіти цієї сигнатури: чи немає там чогось особистого, що пропустило очищення (ім'я в назві функції, шлях у тексті). Якщо є — не схвалювати і виправити очищення в `protocol/src/lib.rs` (з тестом).

```sh
curl -X POST -H "authorization: Bearer $K" $R/admin/v1/signatures/<сигнатура>/approve
```

Відповідь — номер і посилання на issue. Далі сервер сам додає коментар на 2, 5, 10, 25… повторах. Не більше 20 нових issue на день (`429 issue_quota` — завтра). Збій у пакеті openSUSE передаємо в bugzilla.opensuse.org чи upstream вручну, з посиланням на наш issue.

## Позначити виправленим

```sh
curl -X POST -H "authorization: Bearer $K" -H 'content-type: application/json' \
  -d '{"version":"0.2.1"}' $R/admin/v1/signatures/<сигнатура>/fixed
```

Пристрої з цим збоєм отримують `status: fixed` і `fixed_in` (Gloxi зможе сказати «оновіться»). `{"version": null}` знімає позначку.

## Відгуки

- Відповісти: з privacy@ або hello@ на адресу з `contact`, лише про цей відгук.
- Опублікувати (`may_publish: true`): переписати суть у публічний issue **без** пошти й особистих подробиць.
- Видалити (людина попросила листом): `curl -X DELETE -H "authorization: Bearer $K" $R/admin/v1/feedback/<id>`.

## Прохання видалити дані

З пристрою — `POST /v1/forget` з номерами (робить `vilna-reportd` чи Gloxi). Якщо людина пише листом без номерів, ми не можемо знайти її звіти про збої (вони не пов'язані з людиною); відгук шукаємо за поштою в `/admin/v1/feedback` і видаляємо. Усе інше зникає само за 90 днів.
