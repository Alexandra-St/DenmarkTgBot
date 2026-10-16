export const uk = {
  welcome: "Вітаємо у DenmarkTgBot! Оберіть мову:",
  languagePrompt: "Оберіть мову:",
  languageSaved: "Мову збережено.",
  mainMenu: "Оберіть розділ:",
  news: "📰 Новини",
  archive: "📚 Архів",
  settings: "⚙️ Налаштування",
  about: "ℹ️ Про бота",
  language: "🌐 Мова",
  notifications: "🔔 Сповіщення",
  direct: "🟢 Безпосередньо стосуються українців",
  potential: "🟡 Потенційно пов’язані зміни",
  enabled: "✅",
  disabled: "⬜",
  settingsTitle: "⚙️ Налаштування",
  notificationsTitle: "🔔 Налаштування сповіщень",
  languageTitle: "Оберіть мову:",
  back: "⬅️ Назад",
  main: "🏠 Головне меню",
  unavailable: "Цей розділ поки недоступний. Моніторинг ще не запущено.",
  aboutText:
    "DenmarkTgBot — незалежний інформаційний проєкт для українців під тимчасовим захистом у Данії. Планується моніторинг офіційних джерел: Folketinget (https://www.ft.dk/) і Høringsportalen (https://hoeringsportalen.dk/). Моніторинг ще не працює. Інформація не є юридичною консультацією.",
  error: "Не вдалося обробити запит. Спробуйте ще раз.",
} as const;

export type TranslationKey = keyof typeof uk;
