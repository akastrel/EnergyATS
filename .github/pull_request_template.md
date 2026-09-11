## Что изменено

<!-- Кратко: какое поведение/архитектура/исправление меняется. -->

## Проверки

- [ ] Automated tests / CI прошли или для docs-only PR отмечено N/A.
- [ ] Для нового/изменённого поведения есть понятный regression/integration test либо объяснено, почему он не нужен.
- [ ] Если затронута физическая логика, проверен `docs/USER_TESTS_RU.md`.

## Документация — проверить перед merge

Отметить каждый пункт: обновлён либо действительно N/A.

- [ ] Root `README.md` актуален.
- [ ] HA Store `energy_ats/README.md` актуален.
- [ ] HA Documentation `energy_ats/DOCS.md` актуален.
- [ ] `energy_ats/CHANGELOG.md` актуален для release/behavior change.
- [ ] `docs/REQUIREMENTS_RU.md` актуален, если менялось требуемое поведение.
- [ ] `docs/ARCHITECTURE_RU.md` актуален, если менялась ответственность компонентов/runtime flow.
- [ ] `docs/ENTITIES_RU.md` актуален, если менялись HA entities/status/commands.
- [ ] `docs/INSTALL_RU.md` актуален, если менялись config, dependencies, persistence или upgrade path.
- [ ] `docs/PHYSICAL_POWER_TOPOLOGY_RU.md` актуален, если менялась реальная электроустановка.
- [ ] `docs/USER_TESTS_RU.md` актуален, если изменился физически проверяемый сценарий.

**Правило:** не создавать отдельный feature-doc, если та же информация уже имеет canonical место в Requirements / Entities / Install / User Tests. Один факт — один нормативный источник.