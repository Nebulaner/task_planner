from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from datetime import datetime
from time import sleep
from typing import Optional, List, Dict, Any
import json
import os
import re


SCRIPT_VERSION = "strict-flowchart-auth-fix-2026-05-28-v6-main-with-tested-fixes"


class Deadline:
    """Структура данных для хранения информации о дедлайне."""

    def __init__(self, course: str, task: str, due_date: str, source: str = "openedu"):
        self.course = course
        self.task = task
        self.due_date = due_date
        self.source = source

    def to_dict(self) -> Dict[str, str]:
        return {
            "course": self.course,
            "task": self.task,
            "due_date": self.due_date,
            "source": self.source
        }


class FlowchartDeadlineCollector:
    """
    Основной сборщик дедлайнов Openedu.

    Алгоритм соответствует блок-схеме:
    - чтение JSON-конфигурации;
    - запуск Playwright;
    - переход на Openedu;
    - проверка авторизации;
    - вход через СПБПУ при необходимости;
    - переход в "Мои курсы";
    - получение списка курсов;
    - переход в материалы курса;
    - поиск расписания;
    - обработка таблицы расписания;
    - сохранение дедлайнов в JSON;
    - закрытие браузера.

    Дополнительно добавлены исправления, проверенные на вкладке "Завершённые":
    - сохранение сессии в data/storage_state.json;
    - повторное использование сессии;
    - безопасный переход между страницами;
    - устойчивый клик по вкладкам;
    - устойчивый переход в материалы курса;
    - возврат обратно к нужной вкладке курсов.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config

        moodle_config = config.get("moodle", {})
        browser_config = config.get("browser", {})
        parser_config = config.get("parser", {})

        self.username = moodle_config.get("username", "")
        self.password = moodle_config.get("password", "")

        self.browser_type = browser_config.get("type", "chromium")
        self.headless = bool(browser_config.get("headless", False))
        self.storage_state_path = browser_config.get(
            "storage_state_path",
            "data/storage_state.json"
        )

        self.output_dir = parser_config.get("output_dir", "data")
        self.default_year = int(parser_config.get("default_year", 2026))

        # По умолчанию основной файл смотрит вкладку "Текущие".
        # Для проверки завершённых курсов можно поставить "Заверш" в credentials.json.
        self.courses_tab = parser_config.get("courses_tab", "Текущ")

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        self.deadlines: List[Deadline] = []
        self.errors: List[Dict[str, str]] = []

        self.default_timeout = 30000
        self.navigation_timeout = 60000

    def run(self) -> None:
        print("[FLOW] Начало")
        print(f"[INFO] Версия файла: {SCRIPT_VERSION}")
        print(f"[INFO] Целевая вкладка курсов: {self.courses_tab}")

        try:
            self.initialize_playwright_and_open_browser()

            print("[FLOW] Переход на openedu/lms")
            self.go_to_openedu_lms()

            print("[FLOW] Пользователь авторизован?")
            if self.user_is_authorized():
                print("[FLOW] Пользователь авторизован? Да")
                login_successful = True
            else:
                print("[FLOW] Пользователь авторизован? Нет")
                print("[FLOW] Выполнить вход через учетную запись СПБПУ")
                login_successful = self.login_through_spbstu_account()

            print(f"[FLOW] Вход успешен: {'да' if login_successful else 'нет'}")

            if not login_successful:
                self.add_error(
                    source="openedu",
                    stage="auth",
                    message="Вход через учетную запись СПБПУ не выполнен"
                )
                self.write_errors_to_json()
                return

            print("[FLOW] Сохранение авторизованной сессии")
            self.save_storage_state()

            print("[FLOW] Переход в 'Мои курсы' / ожидание загрузки карточек курсов")
            if not self.go_to_my_courses_and_wait_for_cards():
                self.write_all_saved_deadlines_to_json()
                self.write_errors_to_json()
                return

            print("[FLOW] Получение списка курсов")
            courses = self.get_course_list()

            print("[FLOW] Инициализация пустого списка обработанных курсов")
            processed_courses: List[int] = []

            print("[FLOW] Остались необработанные курсы?")
            course_index = 0

            while course_index < len(courses):
                if course_index in processed_courses:
                    course_index += 1
                    continue

                course = courses[course_index]
                course_title = course.get("title", f"Курс {course_index + 1}")

                print("-" * 70)
                print(f"[FLOW] Обработка курса {course_index + 1}/{len(courses)}: {course_title}")

                materials_opened = self.find_materials_button_and_go_to_materials(course_index)

                if not materials_opened:
                    self.add_error(
                        source="openedu",
                        stage="materials",
                        message=f"Кнопка перехода к материалам не найдена или не сработала для курса: {course_title}"
                    )
                    processed_courses.append(course_index)
                    self.return_to_my_courses()
                    course_index += 1
                    continue

                print("[FLOW] Поиск кнопки 'Расписание курса'")
                schedule_button = self.find_schedule_button()

                print("[FLOW] Расписание найдено?")
                if schedule_button is None:
                    print("[FLOW] Расписание найдено? Нет")
                    self.save_screenshot(f"schedule_not_found_course_{course_index + 1}")
                    print("[FLOW] Курс отмечен как обработанный / возврат к списку курсов")
                    processed_courses.append(course_index)
                    self.return_to_my_courses()
                    course_index += 1
                    continue

                print("[FLOW] Расписание найдено? Да")
                print("[FLOW] Переход к расписанию курса")
                schedule_opened = self.go_to_schedule_course(schedule_button)

                if not schedule_opened:
                    processed_courses.append(course_index)
                    self.return_to_my_courses()
                    course_index += 1
                    continue

                print("[FLOW] Переход к обработке таблицы расписания")
                self.process_schedule_table(course_title)

                print("[FLOW] Курс отмечен как обработанный / возврат к списку курсов")
                processed_courses.append(course_index)
                self.return_to_my_courses()
                course_index += 1

            print("[FLOW] Запись в JSON файл всех сохраненных дедлайнов")
            self.write_all_saved_deadlines_to_json()

            if self.errors:
                self.write_errors_to_json()

        except KeyboardInterrupt:
            print("[WARN] Работа прервана пользователем")
            self.add_error("system", "keyboard_interrupt", "Работа прервана пользователем")
            self.write_errors_to_json()

        except Exception as error:
            print(f"[ERROR] Критическая ошибка: {error}")
            self.add_error("system", "critical", str(error))
            self.save_screenshot("critical_error")
            self.write_errors_to_json()

        finally:
            print("[FLOW] Закрытие браузера")
            self.close_browser()
            print("[FLOW] Конец")

    def initialize_playwright_and_open_browser(self) -> None:
        print("[FLOW] Инициализация Playwright / открытие браузера")
        print(f"[INFO] Запуск {self.browser_type.upper()} в {'фоновом' if self.headless else 'обычном'} режиме...")

        self.playwright = sync_playwright().start()
        browser_launcher = getattr(self.playwright, self.browser_type)

        self.browser = browser_launcher.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        if os.path.exists(self.storage_state_path):
            print(f"[INFO] Используется сохранённая сессия: {self.storage_state_path}")
            context_options["storage_state"] = self.storage_state_path
        else:
            print(f"[INFO] Сохранённая сессия не найдена: {self.storage_state_path}")

        try:
            self.context = self.browser.new_context(**context_options)
        except Exception as error:
            print(f"[WARN] Не удалось использовать сохранённую сессию: {error}")
            print("[WARN] Запуск с чистой сессией")

            if "storage_state" in context_options:
                del context_options["storage_state"]

            self.context = self.browser.new_context(**context_options)

        self.page = self.context.new_page()
        self.page.set_default_timeout(self.default_timeout)

    def save_storage_state(self) -> None:
        try:
            storage_dir = os.path.dirname(self.storage_state_path)

            if storage_dir:
                os.makedirs(storage_dir, exist_ok=True)

            self.context.storage_state(path=self.storage_state_path)
            print(f"[SAVE] Сессия сохранена в {self.storage_state_path}")
            print("[INFO] При следующем запуске скрипт попробует войти автоматически через эту сессию")

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="storage_state",
                message=f"Не удалось сохранить сессию: {error}"
            )

    def go_to_openedu_lms(self) -> None:
        print("[INFO] Переход на Openedu...")
        self.safe_go_to_my_courses()
        sleep(3)

    def safe_go_to_my_courses(self) -> bool:
        target_url = "https://openedu.ru/my/courses/"

        try:
            current_url = self.page.url

            if current_url.startswith(target_url):
                self.wait_until_page_stable()
                return True

            self.page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout
            )
            self.wait_until_page_stable()
            return True

        except Exception as error:
            error_text = str(error)

            if (
                "interrupted by another navigation" in error_text
                and "openedu.ru/my/courses" in self.page.url
            ):
                print("[WARN] Повторный переход на ту же страницу проигнорирован")
                self.wait_until_page_stable()
                return True

            self.add_error(
                source="openedu",
                stage="safe_go_to_my_courses",
                message=f"Не удалось перейти в 'Мои курсы': {error}"
            )
            return False

    def wait_until_page_stable(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        sleep(2)

    def user_is_authorized(self) -> bool:
        current_url = self.page.url.lower()
        print(f"[DEBUG] Текущий URL при проверке авторизации: {self.page.url}")

        unauthorized_url_markers = [
            "sso.openedu.ru",
            "/login",
            "/auth",
            "openid-connect",
            "keycloak",
            "realms/openedu"
        ]

        if any(marker in current_url for marker in unauthorized_url_markers):
            print("[DEBUG] Найдены признаки страницы авторизации в URL")
            return False

        authorized_indicators = [
            "text=Личный кабинет",
            "text=Мои курсы",
            "text=Мои программы",
            "text=Выйти",
            "text=Профиль"
        ]

        for selector in authorized_indicators:
            try:
                if self.page.locator(selector).first.is_visible(timeout=2500):
                    print(f"[DEBUG] Найден признак авторизованной страницы: {selector}")
                    return True
            except Exception:
                pass

        course_indicators = [
            "div.ed-product-card",
            ".ed-product-card",
            "text=К материалам курса",
            "text=Материалы курса",
            "text=Перейти к курсу",
            "text=Продолжить обучение"
        ]

        for selector in course_indicators:
            try:
                if self.page.locator(selector).first.is_visible(timeout=2500):
                    print(f"[DEBUG] Найден признак курсов: {selector}")
                    return True
            except Exception:
                pass

        login_selectors = [
            "input[name='username']",
            "input[name='login']",
            "input[name='email']",
            "input[type='password']",
            "button:has-text('Войти')",
            "a:has-text('Регистрация')",
            "text=Логин или e-mail",
            "text=Пароль"
        ]

        for selector in login_selectors:
            try:
                if self.page.locator(selector).first.is_visible(timeout=1500):
                    print(f"[DEBUG] Найден признак формы входа: {selector}")
                    return False
            except Exception:
                pass

        print("[DEBUG] Надежных признаков авторизации не найдено")
        return False

    def login_through_spbstu_account(self) -> bool:
        if not self.username or not self.password:
            self.add_error(
                source="openedu",
                stage="credentials",
                message="В credentials.json не указаны username/password"
            )
            return False

        try:
            print("[INFO] Поиск кнопки входа через Политех / СПБПУ...")

            polytech_clicked = self.click_polytech_login_button()

            if not polytech_clicked:
                print("[WARN] Кнопка Политех не найдена. Пробую обычную форму Openedu...")
                self.fill_any_visible_login_form()

            sleep(3)

            if self.fill_spbstu_login_form_if_present():
                print("[INFO] Форма СПБПУ заполнена, ожидание результата входа...")
                sleep(10)

            if self.fill_any_visible_login_form():
                print("[INFO] Форма Openedu заполнена, ожидание результата входа...")
                sleep(10)

            print("[INFO] Проверка результата автоматической авторизации...")

            for _ in range(30):
                try:
                    self.wait_until_page_stable()
                    if self.user_is_authorized():
                        print("[OK] Автоматическая авторизация успешна")
                        return True
                except Exception:
                    pass

                sleep(1)

            print("[WARN] Автоматический вход не завершился успешно")
            self.save_screenshot("auth_auto_failed_before_manual")

            print()
            print("=" * 80)
            print("[ACTION REQUIRED]")
            print("В открытом окне браузера выполни вход вручную.")
            print("Если нужно, введи логин/пароль, пройди подтверждение или капчу.")
            print("Когда окажешься в личном кабинете Openedu или на странице 'Мои курсы',")
            print("вернись в PowerShell и нажми Enter.")
            print("=" * 80)
            input("Нажми Enter после ручного входа...")

            print("[INFO] Проверка результата ручной авторизации...")

            self.wait_until_page_stable()

            if self.user_is_authorized():
                print("[OK] Ручная авторизация успешна")
                return True

            if self.safe_go_to_my_courses():
                if self.user_is_authorized():
                    print("[OK] Ручная авторизация успешна после перехода в 'Мои курсы'")
                    return True

            self.add_error(
                source="openedu",
                stage="manual_login",
                message="После ручного входа пользователь всё ещё не авторизован"
            )
            self.save_screenshot("manual_login_failed")
            return False

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="login",
                message=f"Ошибка при входе через СПБПУ: {error}"
            )
            self.save_screenshot("login_error")
            return False

    def click_polytech_login_button(self) -> bool:
        selectors = [
            "button:has-text('Политех')",
            "a:has-text('Политех')",
            "div:has-text('Политех')",
            "span:has-text('Политех')",
            "xpath=//*[contains(normalize-space(), 'Политех')]",
            "xpath=//*[contains(normalize-space(), 'СПБПУ')]",
            "xpath=//*[contains(normalize-space(), 'spbstu')]",
            "xpath=//a[contains(@href, 'spbstu')]"
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=3000):
                    locator.scroll_into_view_if_needed(timeout=3000)
                    locator.click(timeout=5000)
                    print(f"[OK] Нажата кнопка входа через Политех/СПБПУ: {selector}")
                    return True
            except Exception:
                continue

        return False

    def fill_spbstu_login_form_if_present(self) -> bool:
        possible_user_selectors = [
            "#user",
            "input#user",
            "input[name='user']",
            "input[name='username']",
            "input[type='text']",
            "input[type='email']"
        ]

        possible_password_selectors = [
            "#password",
            "input#password",
            "input[name='password']",
            "input[type='password']"
        ]

        user_selector = self.find_visible_selector(possible_user_selectors, timeout=5000)
        password_selector = self.find_visible_selector(possible_password_selectors, timeout=5000)

        if user_selector is None or password_selector is None:
            return False

        print("[INFO] Заполнение формы учетной записи СПБПУ...")

        try:
            self.page.fill(user_selector, self.username)
            self.page.fill(password_selector, self.password)
        except Exception as error:
            self.add_error(
                source="openedu",
                stage="spbstu_form_fill",
                message=f"Не удалось заполнить форму СПБПУ: {error}"
            )
            return False

        submit_selectors = [
            "#doLogin",
            "button#doLogin",
            "button#login",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Войти')",
            "input[value='Войти']"
        ]

        submit_selector = self.find_visible_selector(submit_selectors, timeout=5000)

        try:
            if submit_selector:
                self.page.click(submit_selector, timeout=5000)
                print("[OK] Нажата кнопка входа")
            else:
                self.page.keyboard.press("Enter")
                print("[OK] Отправка формы через Enter")

            return True

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="spbstu_submit",
                message=f"Не удалось отправить форму СПБПУ: {error}"
            )
            return False

    def fill_any_visible_login_form(self) -> bool:
        possible_login_selectors = [
            "input[name='login']",
            "input[name='email']",
            "input[name='username']",
            "input[type='email']",
            "input[type='text']"
        ]

        possible_password_selectors = [
            "input[name='password']",
            "input[type='password']"
        ]

        login_selector = self.find_visible_selector(possible_login_selectors, timeout=3000)
        password_selector = self.find_visible_selector(possible_password_selectors, timeout=3000)

        if login_selector is None or password_selector is None:
            return False

        print("[INFO] Заполнение видимой формы входа Openedu...")

        try:
            self.page.fill(login_selector, self.username)
            self.page.fill(password_selector, self.password)
        except Exception as error:
            self.add_error(
                source="openedu",
                stage="openedu_form_fill",
                message=f"Не удалось заполнить форму Openedu: {error}"
            )
            return False

        submit_selectors = [
            "button[type='submit']",
            "button:has-text('Войти')",
            "input[type='submit']",
            "input[value='Войти']"
        ]

        submit_selector = self.find_visible_selector(submit_selectors, timeout=3000)

        try:
            if submit_selector:
                self.page.click(submit_selector, timeout=5000)
                print("[OK] Нажата кнопка входа Openedu")
            else:
                self.page.keyboard.press("Enter")
                print("[OK] Отправка формы Openedu через Enter")

            return True

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="openedu_submit",
                message=f"Не удалось отправить форму Openedu: {error}"
            )
            return False

    def find_visible_selector(self, selectors: List[str], timeout: int = 3000) -> Optional[str]:
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=timeout):
                    return selector
            except Exception:
                continue
        return None

    def go_to_my_courses_and_wait_for_cards(self) -> bool:
        try:
            if not self.safe_go_to_my_courses():
                return False

            sleep(5)

            if not self.user_is_authorized():
                self.add_error(
                    source="openedu",
                    stage="courses",
                    message="После входа пользователь снова оказался на странице авторизации"
                )
                self.save_screenshot("openedu_not_authorized_on_courses")
                return False

            print(f"[FLOW] Переход во вкладку курсов: {self.courses_tab}")

            if not self.open_courses_tab(self.courses_tab):
                self.add_error(
                    source="openedu",
                    stage="courses_tab",
                    message=f"Не удалось открыть вкладку курсов: {self.courses_tab}"
                )
                self.save_screenshot("courses_tab_not_found")
                return False

            print("[FLOW] Ожидание карточек курсов")

            if self.wait_for_course_cards():
                return True

            try:
                if self.page.locator("text=Мои курсы").first.is_visible(timeout=3000):
                    print("[INFO] Страница 'Мои курсы' открыта, но карточек курсов на выбранной вкладке не найдено")
                    print("[INFO] Это не ошибка, если на выбранной вкладке нет курсов")
                    return True
            except Exception:
                pass

            self.add_error(
                source="openedu",
                stage="courses",
                message="Страница 'Мои курсы' открыта, но карточки курсов не обнаружены"
            )
            self.save_screenshot("openedu_no_courses")
            return False

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="courses",
                message=f"Ошибка перехода в 'Мои курсы': {error}"
            )
            self.save_screenshot("openedu_courses_error")
            return False

    def open_courses_tab(self, tab_fragment: str) -> bool:
        """
        Надёжно открывает вкладку курсов по фрагменту текста:
        'Текущ', 'Будущ', 'Заверш', 'Сертифик', 'Избран', 'Подпис'.

        На Openedu вкладки могут быть div, а не button/a.
        Поэтому ищем видимые элементы с нужным текстом и кликаем в центр.
        """

        try:
            candidates = self.page.evaluate(
                """
                (tabFragment) => {
                    const elements = Array.from(document.querySelectorAll("body *"));

                    return elements
                        .map((el, index) => {
                            const text = (el.innerText || el.textContent || "")
                                .replace(/\\s+/g, " ")
                                .trim();

                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);

                            const visible =
                                rect.width > 0 &&
                                rect.height > 0 &&
                                style.visibility !== "hidden" &&
                                style.display !== "none" &&
                                style.opacity !== "0";

                            return {
                                index,
                                text,
                                tag: el.tagName,
                                className: typeof el.className === "string" ? el.className : "",
                                x: rect.x,
                                y: rect.y,
                                width: rect.width,
                                height: rect.height,
                                area: rect.width * rect.height,
                                visible
                            };
                        })
                        .filter(item =>
                            item.visible &&
                            item.text.includes(tabFragment) &&
                            item.width > 20 &&
                            item.height > 10
                        )
                        .sort((a, b) => {
                            const aLooksLikeTab =
                                a.text.length <= 50 &&
                                a.y > 100 &&
                                a.y < 380;

                            const bLooksLikeTab =
                                b.text.length <= 50 &&
                                b.y > 100 &&
                                b.y < 380;

                            if (aLooksLikeTab && !bLooksLikeTab) return -1;
                            if (!aLooksLikeTab && bLooksLikeTab) return 1;

                            return a.area - b.area;
                        });
                }
                """,
                tab_fragment
            )

            if not candidates:
                print(f"[WARN] Не найден ни один видимый элемент с текстом '{tab_fragment}'")
                return False

            print(f"[DEBUG] Кандидаты вкладки '{tab_fragment}':")
            for item in candidates[:10]:
                print(
                    f"  TAG={item['tag']} "
                    f"TEXT='{item['text']}' "
                    f"CLASS='{item['className']}' "
                    f"RECT=({item['x']}, {item['y']}, {item['width']}, {item['height']})"
                )

            for candidate in candidates[:10]:
                x = candidate["x"] + candidate["width"] / 2
                y = candidate["y"] + candidate["height"] / 2

                print(f"[INFO] Пробую кликнуть по вкладке '{tab_fragment}' в точке x={x}, y={y}")

                self.page.mouse.click(x, y)
                sleep(4)
                self.wait_until_page_stable()
                sleep(2)

                self.scroll_page_to_load_everything()

                if self.course_tab_seems_active(tab_fragment):
                    print(f"[OK] Вкладка '{tab_fragment}' визуально похожа на активную")
                    return True

                buttons = self.find_materials_buttons()

                if buttons:
                    print(f"[OK] После клика найдены кнопки/ссылки курсов на вкладке '{tab_fragment}'")
                    return True

                print("[WARN] После клика карточки/кнопки курсов пока не найдены, пробую следующий кандидат")

            print(f"[WARN] Не удалось открыть вкладку '{tab_fragment}' кликом по кандидатам")
            return False

        except Exception as error:
            print(f"[ERROR] Ошибка открытия вкладки '{tab_fragment}': {error}")
            return False

    def course_tab_seems_active(self, tab_fragment: str) -> bool:
        try:
            cards = self.find_course_cards()

            if cards:
                return True
        except Exception:
            pass

        try:
            buttons = self.find_materials_buttons()

            if buttons:
                return True
        except Exception:
            pass

        try:
            page_text = self.page.locator("body").inner_text(timeout=5000)

            if tab_fragment not in page_text:
                return False

            if "Мои курсы" in page_text:
                return True

        except Exception:
            pass

        return False

    def scroll_page_to_load_everything(self) -> None:
        try:
            previous_height = 0

            for _ in range(8):
                current_height = self.page.evaluate("document.body.scrollHeight")

                if current_height == previous_height:
                    break

                previous_height = current_height
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                sleep(2)

            self.page.evaluate("window.scrollTo(0, 0)")
            sleep(1)

        except Exception:
            pass

    def wait_for_course_cards(self) -> bool:
        self.scroll_page_to_load_everything()

        card_selectors = [
            "div.ed-product-card",
            ".ed-product-card",
            "article:has-text('К материалам курса')",
            "div:has-text('К материалам курса')",
            "article:has-text('Материалы курса')",
            "div:has-text('Материалы курса')",
            "article:has-text('Перейти к курсу')",
            "div:has-text('Перейти к курсу')",
            "article:has-text('Открыть курс')",
            "div:has-text('Открыть курс')",
            "article:has-text('Продолжить обучение')",
            "div:has-text('Продолжить обучение')",
        ]

        for selector in card_selectors:
            try:
                self.page.wait_for_selector(selector, timeout=10000)
                count = self.page.locator(selector).count()

                if count > 0:
                    print(f"[OK] Найдены карточки/блоки курсов: {selector}, количество: {count}")
                    return True

            except PlaywrightTimeout:
                continue
            except Exception:
                continue

        material_buttons = self.find_materials_buttons()

        if material_buttons:
            print(f"[OK] Карточки не распознаны по классам, но найдены кнопки перехода к курсам: {len(material_buttons)}")
            return True

        return False

    def find_course_cards(self):
        card_selectors = [
            "div.ed-product-card",
            ".ed-product-card",
            "article:has-text('К материалам курса')",
            "article:has-text('Материалы курса')",
            "article:has-text('Перейти к курсу')",
            "article:has-text('Открыть курс')",
            "article:has-text('Продолжить обучение')"
        ]

        for selector in card_selectors:
            try:
                cards = self.page.locator(selector).all()

                if cards:
                    return cards
            except Exception:
                continue

        return []

    def get_course_list(self) -> List[Dict[str, str]]:
        courses: List[Dict[str, str]] = []

        cards = self.find_course_cards()

        print(f"[INFO] Найдено карточек курсов: {len(cards)}")

        for index, card in enumerate(cards):
            title = ""

            title_selectors = [
                "div.ed-product-card__header__title span",
                ".ed-product-card__header__title",
                "[class*='title']",
                "h1",
                "h2",
                "h3",
                "a",
                "span"
            ]

            for selector in title_selectors:
                try:
                    text = card.locator(selector).first.text_content(timeout=2000)
                    if text and text.strip():
                        title = self.clean_text(text)
                        break
                except Exception:
                    continue

            if not title:
                try:
                    full_text = card.text_content(timeout=2000)
                    title = self.extract_course_title_from_text(full_text)
                except Exception:
                    pass

            if not title:
                title = f"Курс {index + 1}"

            courses.append({
                "index": str(index),
                "title": title
            })

        if courses:
            print(f"[INFO] Получено курсов: {len(courses)}")
            return courses

        print("[WARN] Стандартные карточки не найдены, используется fallback по кнопкам перехода к курсам")

        buttons = self.find_materials_buttons()
        fallback_courses: List[Dict[str, str]] = []

        for index, button in enumerate(buttons):
            title = self.extract_course_title_near_button(button)

            if not title:
                title = f"Курс {index + 1}"

            fallback_courses.append({
                "index": str(index),
                "title": title
            })

        print(f"[INFO] Получено курсов через fallback: {len(fallback_courses)}")
        return fallback_courses

    def extract_course_title_from_text(self, text: str) -> str:
        if not text:
            return ""

        forbidden = [
            "К материалам курса",
            "Материалы курса",
            "Продолжить обучение",
            "Перейти к курсу",
            "Открыть курс",
            "Смотреть курс",
            "Найти"
        ]

        lines = [
            self.clean_text(line)
            for line in text.splitlines()
            if self.clean_text(line)
        ]

        for line in lines:
            if len(line) > 5 and not any(fragment in line for fragment in forbidden):
                return line

        return ""

    def find_materials_button_and_go_to_materials(self, course_index: int) -> bool:
        """
        Переход к материалам курса.

        Исправление:
        если клик вызвал переход на apps.openedu.ru, но Playwright выдал timeout,
        считаем переход успешным, потому что страница курса фактически открылась.
        """

        print("[FLOW] Поиск кнопки 'К материалам курса' / переход в раздел материалов")

        target = self.find_materials_button_for_course(course_index)

        if target is None:
            print(f"[WARN] Кнопка материалов не найдена для индекса курса {course_index}")
            self.save_screenshot(f"no_material_button_{course_index + 1}")
            return False

        try:
            old_url = self.page.url

            try:
                target.scroll_into_view_if_needed(timeout=5000)
                sleep(1)
            except Exception:
                pass

            try:
                button_text = target.text_content(timeout=2000) or ""
                button_text = self.clean_text(button_text)
                print(f"[INFO] Нажимаю кнопку курса {course_index + 1}: {button_text}")
            except Exception:
                print(f"[INFO] Нажимаю кнопку курса {course_index + 1}")

            click_error = None

            try:
                target.click(timeout=7000)
            except Exception as error:
                click_error = error
                print(f"[WARN] Клик дал исключение, проверяю URL: {error}")

            self.wait_until_page_stable()
            sleep(5)

            current_url = self.page.url
            print(f"[DEBUG] URL после клика: {current_url}")

            course_url_markers = [
                "apps.openedu.ru/learning/course",
                "/learning/course/",
                "course-v1:"
            ]

            if any(marker in current_url for marker in course_url_markers):
                print("[OK] Выполнен переход к материалам курса")
                return True

            if current_url != old_url and "openedu.ru" in current_url:
                print("[OK] URL изменился после клика, считаю переход успешным")
                return True

            if click_error:
                self.add_error(
                    source="openedu",
                    stage="materials_click",
                    message=f"Клик по кнопке материалов не привел к переходу: {click_error}"
                )
                self.save_screenshot("materials_click_error")
                return False

            print("[WARN] После клика URL не изменился")
            self.save_screenshot("materials_no_navigation")
            return False

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="materials_click",
                message=f"Ошибка клика по кнопке материалов курса: {error}"
            )
            self.save_screenshot("materials_click_error")
            return False

    def find_materials_button_for_course(self, course_index: int):
        cards = self.find_course_cards()

        if course_index < len(cards):
            card = cards[course_index]

            selectors = [
                "a:has-text('К материалам курса')",
                "button:has-text('К материалам курса')",
                "a:has-text('Материалы курса')",
                "button:has-text('Материалы курса')",
                "a:has-text('Перейти к курсу')",
                "button:has-text('Перейти к курсу')",
                "a:has-text('Открыть курс')",
                "button:has-text('Открыть курс')",
                "a:has-text('Смотреть курс')",
                "button:has-text('Смотреть курс')",
                "a:has-text('Продолжить обучение')",
                "button:has-text('Продолжить обучение')",
                "a[href*='apps.openedu.ru/learning/course']",
                "a[href*='/learning/course/']",
                "a[href*='course-v1']"
            ]

            for selector in selectors:
                try:
                    locator = card.locator(selector).first
                    if locator.count() > 0 and locator.is_visible(timeout=2000):
                        return locator
                except Exception:
                    continue

        buttons = self.find_materials_buttons()

        if course_index < len(buttons):
            return buttons[course_index]

        return None

    def find_materials_buttons(self):
        selectors = [
            "a:has-text('К материалам курса')",
            "button:has-text('К материалам курса')",
            "a:has-text('Материалы курса')",
            "button:has-text('Материалы курса')",
            "a:has-text('Перейти к курсу')",
            "button:has-text('Перейти к курсу')",
            "a:has-text('Открыть курс')",
            "button:has-text('Открыть курс')",
            "a:has-text('Смотреть курс')",
            "button:has-text('Смотреть курс')",
            "a:has-text('Продолжить обучение')",
            "button:has-text('Продолжить обучение')",
            "a[href*='apps.openedu.ru/learning/course']",
            "a[href*='/learning/course/']",
            "a[href*='course-v1']"
        ]

        visible_buttons = []
        seen = set()

        for selector in selectors:
            try:
                locators = self.page.locator(selector).all()

                for locator in locators:
                    try:
                        if not locator.is_visible(timeout=1000):
                            continue

                        text = locator.text_content(timeout=1000) or ""
                        text = self.clean_text(text)

                        href = ""
                        try:
                            href = locator.get_attribute("href") or ""
                        except Exception:
                            pass

                        key = f"{text}::{href}"

                        if key in seen:
                            continue

                        seen.add(key)
                        visible_buttons.append(locator)

                    except Exception:
                        continue

            except Exception:
                continue

        if visible_buttons:
            return visible_buttons

        xpath_parts = [
            "contains(normalize-space(), 'К материалам курса')",
            "contains(normalize-space(), 'Материалы курса')",
            "contains(normalize-space(), 'Продолжить обучение')",
            "contains(normalize-space(), 'Перейти к курсу')",
            "contains(normalize-space(), 'Открыть курс')",
            "contains(normalize-space(), 'Смотреть курс')",
        ]

        xpath = "//*[" + " or ".join(xpath_parts) + "]"

        try:
            raw_buttons = self.page.locator(f"xpath={xpath}").all()
        except Exception:
            return []

        for button in raw_buttons:
            try:
                if button.is_visible(timeout=1000):
                    visible_buttons.append(button)
            except Exception:
                continue

        return visible_buttons

    def extract_course_title_near_button(self, button) -> Optional[str]:
        ancestor_selectors = [
            "xpath=ancestor::*[contains(@class, 'ed-product-card')][1]",
            "xpath=ancestor::*[contains(@class, 'card')][1]",
            "xpath=ancestor::article[1]",
            "xpath=ancestor::div[1]",
            "xpath=ancestor::section[1]",
        ]

        title_selectors = [
            ".ed-product-card__header__title",
            "[class*='title']",
            "h1",
            "h2",
            "h3",
            "a",
            "span",
        ]

        for ancestor_selector in ancestor_selectors:
            try:
                ancestor = button.locator(ancestor_selector).first

                if ancestor.count() == 0:
                    continue

                for title_selector in title_selectors:
                    try:
                        title_text = ancestor.locator(title_selector).first.text_content(timeout=2000)

                        if title_text and title_text.strip():
                            cleaned = self.clean_text(title_text)

                            forbidden_fragments = [
                                "К материалам курса",
                                "Материалы курса",
                                "Продолжить обучение",
                                "Перейти к курсу",
                                "Открыть курс",
                                "Смотреть курс",
                            ]

                            if cleaned and not any(fragment in cleaned for fragment in forbidden_fragments):
                                return cleaned

                    except Exception:
                        continue

                full_text = ancestor.text_content(timeout=2000)
                title = self.extract_course_title_from_text(full_text)

                if title:
                    return title

            except Exception:
                continue

        return None

    def find_schedule_button(self):
        selectors = [
            "a.nav-link:has-text('Расписание курса')",
            "a:has-text('Расписание курса')",
            "button:has-text('Расписание курса')",
            "a:has-text('Расписание')",
            "button:has-text('Расписание')",
            "a:has-text('Даты')",
            "button:has-text('Даты')",
            "a:has-text('Сроки')",
            "button:has-text('Сроки')",
            "a:has-text('Календарь')",
            "button:has-text('Календарь')",
            "a[href*='dates']",
            "a[href*='schedule']",
            "a[href*='calendar']",
            "a[href*='static_tab']",
            "xpath=//*[contains(normalize-space(), 'Расписание курса')]",
            "xpath=//*[contains(normalize-space(), 'Расписание')]",
            "xpath=//*[contains(normalize-space(), 'Даты')]",
            "xpath=//*[contains(normalize-space(), 'Сроки')]",
            "xpath=//*[contains(normalize-space(), 'Календарь')]"
        ]

        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.count() > 0 and locator.is_visible(timeout=3000):
                    print(f"[OK] Кнопка расписания найдена: {selector}")
                    return locator
            except Exception:
                continue

        return None

    def go_to_schedule_course(self, schedule_button) -> bool:
        try:
            old_url = self.page.url

            try:
                schedule_button.scroll_into_view_if_needed(timeout=5000)
                sleep(1)
            except Exception:
                pass

            click_error = None

            try:
                schedule_button.click(timeout=5000)
            except Exception as error:
                click_error = error
                print(f"[WARN] Клик по расписанию дал исключение, проверяю URL: {error}")

            self.wait_until_page_stable()
            sleep(5)

            current_url = self.page.url

            if current_url != old_url:
                print("[OK] Выполнен переход к расписанию курса")
                return True

            if click_error:
                self.add_error(
                    source="openedu",
                    stage="schedule_click",
                    message=f"Клик по расписанию не привел к переходу: {click_error}"
                )
                self.save_screenshot("schedule_click_error")
                return False

            print("[OK] Клик по расписанию выполнен без смены URL")
            return True

        except Exception as error:
            self.add_error(
                source="openedu",
                stage="schedule_click",
                message=f"Ошибка перехода к расписанию курса: {error}"
            )
            self.save_screenshot("schedule_click_error")
            return False

    def process_schedule_table(self, course_title: str) -> None:
        try:
            self.page.wait_for_selector("table", timeout=15000)
        except PlaywrightTimeout:
            self.add_error(
                source="openedu",
                stage="schedule_table",
                message=f"Таблица расписания не найдена для курса: {course_title}"
            )
            self.save_screenshot("schedule_table_not_found")
            return

        tables = self.page.locator("table").all()

        for table_index, table in enumerate(tables):
            rows = table.locator("tr").all()

            if len(rows) < 2:
                continue

            print(f"[INFO] Обработка таблицы {table_index + 1}, строк: {len(rows)}")
            print("[FLOW] Инициализация пустого списка обработанных строк")

            processed_rows: List[int] = []
            row_index = 1

            print("[FLOW] Остались необработанные строки?")
            while row_index < len(rows):
                if row_index in processed_rows:
                    row_index += 1
                    continue

                row = rows[row_index]

                print("[FLOW] Извлечение: название задания + дата дедлайна")
                extracted = self.extract_task_and_deadline_date(row)

                if extracted is None:
                    processed_rows.append(row_index)
                    row_index += 1
                    continue

                task_title, raw_date = extracted

                print("[FLOW] Дата корректна?")
                formatted_date = self.check_and_format_date(raw_date)

                if formatted_date is None:
                    print("[FLOW] Дата корректна? Нет")
                    processed_rows.append(row_index)
                    row_index += 1
                    continue

                print("[FLOW] Дата корректна? Да")
                print("[FLOW] Форматирование даты в YYYY-MM-DD")
                print("[FLOW] Сохранение дедлайна: course, task, due_date, source")

                self.deadlines.append(
                    Deadline(
                        course=course_title,
                        task=task_title,
                        due_date=formatted_date,
                        source="openedu"
                    )
                )

                print(f"[SAVE] {course_title} | {task_title} | {formatted_date}")

                processed_rows.append(row_index)
                row_index += 1

    def extract_task_and_deadline_date(self, row) -> Optional[tuple]:
        cells = row.locator("td").all()

        if not cells:
            return None

        cell_texts = []

        for cell in cells:
            try:
                text = cell.text_content(timeout=2000)
                text = self.clean_text(text)
                if text:
                    cell_texts.append(text)
            except Exception:
                continue

        if not cell_texts:
            return None

        task_title = cell_texts[0]
        raw_date = None

        for text in reversed(cell_texts):
            if self.looks_like_date(text):
                raw_date = text
                break

        if not task_title or not raw_date:
            return None

        return task_title, raw_date

    def looks_like_date(self, text: str) -> bool:
        if not text:
            return False

        lowered = text.lower()

        if lowered in ["-", "—", "нет", "none"]:
            return False

        if "инд." in lowered or "индивидуально" in lowered:
            return False

        patterns = [
            r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",
            r"\b\d{1,2}\.\d{1,2}\b",
            r"\b\d{4}-\d{1,2}-\d{1,2}\b"
        ]

        return any(re.search(pattern, text) for pattern in patterns)

    def check_and_format_date(self, raw_date: str) -> Optional[str]:
        if not raw_date:
            return None

        raw_date = self.clean_text(raw_date)

        iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", raw_date)
        if iso_match:
            year = int(iso_match.group(1))
            month = int(iso_match.group(2))
            day = int(iso_match.group(3))
            return self.safe_format_date(year, month, day)

        dot_match = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", raw_date)
        if dot_match:
            day = int(dot_match.group(1))
            month = int(dot_match.group(2))

            if dot_match.group(3):
                year_text = dot_match.group(3)
                if len(year_text) == 2:
                    year = int("20" + year_text)
                else:
                    year = int(year_text)
            else:
                year = self.default_year

            return self.safe_format_date(year, month, day)

        return None

    def safe_format_date(self, year: int, month: int, day: int) -> Optional[str]:
        try:
            date_object = datetime(year=year, month=month, day=day)
            return date_object.strftime("%Y-%m-%d")
        except ValueError:
            return None

    def return_to_my_courses(self) -> None:
        """
        Возврат не просто в 'Мои курсы', а именно обратно к выбранной вкладке.
        """

        print(f"[FLOW] Возврат к списку курсов, вкладка: {self.courses_tab}")

        self.safe_go_to_my_courses()
        sleep(4)

        opened = self.open_courses_tab(self.courses_tab)

        if not opened:
            print(f"[WARN] Не удалось повторно открыть вкладку '{self.courses_tab}' после возврата")
            self.save_screenshot("courses_tab_reopen_failed")
            return

        sleep(4)

        if not self.wait_for_course_cards():
            print("[WARN] После возврата карточки курсов не найдены")
            self.save_screenshot("course_cards_after_return_not_found")

    def write_all_saved_deadlines_to_json(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(self.output_dir, f"deadlines_{timestamp}.json")

        data = [deadline.to_dict() for deadline in self.deadlines]

        with open(filename, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        if self.deadlines:
            print(f"[SAVE] Дедлайны сохранены в {filename}")
        else:
            print("[INFO] Дедлайнов не найдено")
            print(f"[SAVE] Пустой список дедлайнов сохранен в {filename}")

    def add_error(self, source: str, stage: str, message: str) -> None:
        error = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "stage": stage,
            "message": message,
            "url": self.page.url if self.page else ""
        }

        self.errors.append(error)
        print(f"[ERROR] {source} | {stage}: {message}")

    def write_errors_to_json(self) -> None:
        if not self.errors:
            return

        errors_dir = os.path.join(self.output_dir, "errors")
        os.makedirs(errors_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(errors_dir, f"errors_{timestamp}.json")

        with open(filename, "w", encoding="utf-8") as file:
            json.dump(self.errors, file, ensure_ascii=False, indent=2)

        print(f"[SAVE] Ошибки сохранены в {filename}")

    def save_screenshot(self, name: str) -> None:
        if not self.page:
            return

        screenshots_dir = os.path.join(self.output_dir, "screenshots")
        os.makedirs(screenshots_dir, exist_ok=True)

        filename = os.path.join(screenshots_dir, f"{name}.png")

        try:
            self.page.screenshot(path=filename, full_page=True)
            print(f"[SAVE] Скриншот ошибки сохранен: {filename}")
        except Exception:
            pass

    def close_browser(self) -> None:
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.playwright:
                self.playwright.stop()

        print("[INFO] Браузер закрыт")

    @staticmethod
    def clean_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()


def load_configuration(filepath: str = "misc/credentials.json") -> Dict[str, Any]:
    print("[FLOW] Получение JSON файла конфигурации, содержащего идентификаторы доступа учетной записи СПБПУ")

    if not os.path.exists(filepath):
        print(f"[ERROR] Файл конфигурации не найден: {filepath}")
        print("Создайте файл misc/credentials.json со структурой:")
        print(json.dumps({
            "moodle": {
                "username": "your.email@edu.spbstu.ru",
                "password": "your_password"
            },
            "browser": {
                "type": "chromium",
                "headless": False,
                "storage_state_path": "data/storage_state.json"
            },
            "parser": {
                "output_dir": "data",
                "default_year": 2026,
                "courses_tab": "Текущ"
            }
        }, ensure_ascii=False, indent=2))
        raise FileNotFoundError(filepath)

    with open(filepath, "r", encoding="utf-8") as file:
        config = json.load(file)

    return config


if __name__ == "__main__":
    configuration = load_configuration()
    collector = FlowchartDeadlineCollector(configuration)
    collector.run()
