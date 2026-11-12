// All user-facing copy lives here (Ukrainian). Extend per feature; never inline strings in components.
export const t = {
  appName: "Uptime Monitor",
  errors: {
    network: "Немає з'єднання з сервером. Перевірте інтернет і спробуйте ще раз.",
    server: "Сервер тимчасово недоступний. Спробуйте за хвилину.",
    session: "Сесія завершилась. Увійдіть знову.",
    generic: "Щось пішло не так. Спробуйте ще раз.",
  },
  status: {
    up: "Працює",
    down: "Недоступний",
    unknown: "Немає даних",
    maintenance: "Обслуговування",
    paused: "Пауза",
    degraded: "Нестабільно",
  },
  nav: {
    monitors: "Монітори",
    statusPages: "Статус-сторінки",
    reports: "Звіти",
    security: "Безпека",
    main: "Головна навігація",
    bottom: "Нижня навігація",
    logout: "Вийти",
    theme: "Тема",
    themeLight: "Світла",
    themeDark: "Темна",
    themeSystem: "Системна",
  },
  common: {
    retry: "Повторити",
    noData: "немає даних",
    loading: "Завантаження",
    comingSoon: "Цей розділ у розробці.",
  },
  notFound: {
    title: "Сторінку не знайдено",
    home: "На головну",
  },
} as const;
