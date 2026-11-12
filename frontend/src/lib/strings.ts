// All user-facing copy lives here (Ukrainian). Extend per feature; never inline strings in components.
export const t = {
  appName: "Uptime Monitor",
  errors: {
    network: "Немає з'єднання з сервером. Перевірте інтернет і спробуйте ще раз.",
    server: "Сервер тимчасово недоступний. Спробуйте за хвилину.",
    session: "Сесія завершилась. Увійдіть знову.",
    generic: "Щось пішло не так. Спробуйте ще раз.",
  },
} as const;
