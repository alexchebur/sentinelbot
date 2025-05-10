# test.py
import os
import random
import xml.etree.ElementTree as ET
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext

class QuizHandler:
    def __init__(self):
        self.questions = self._load_questions()
        self.rank_titles = {
            0: "Новичок",
            1: "Стажёр",
            3: "Специалист",
            6: "Эксперт",
            9: "Мастер комплаенса"
        }

    def _load_questions(self):
        """Загрузка вопросов из XML-файла"""
        path = os.path.join('data', 'qa_quiz.xml')
        tree = ET.parse(path)
        root = tree.getroot()
        
        questions = []
        for question_elem in root.findall('question'):
            question_text = question_elem.find('text').text
            answers = []
            correct_idx = 0
            for idx, answer_elem in enumerate(question_elem.findall('answer')):
                answers.append(answer_elem.text)
                if answer_elem.get('correct') == 'true':
                    correct_idx = idx
            questions.append({
                'text': question_text,
                'answers': answers,
                'correct': correct_idx
            })
        return questions

    def start_quiz(self, update, context: CallbackContext):
        """Начало нового теста"""
        user_id = update.effective_user.id
        context.user_data[user_id] = {
            'current_question': 0,
            'score': 0,
            'quiz_questions': random.sample(self.questions, 10)
        }
        self._send_question(update, context)

    def _send_question(self, update, context):
        """Отправка вопроса пользователю"""
        user_id = update.effective_user.id
        data = context.user_data[user_id]
        question = data['quiz_questions'][data['current_question']]

        keyboard = [
            [InlineKeyboardButton(ans, callback_data=str(idx))]
            for idx, ans in enumerate(question['answers'])
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Вопрос {data['current_question'] + 1}/10:\n{question['text']}",
            reply_markup=reply_markup
        )

    def handle_answer(self, update, context):
        """Обработка ответа пользователя"""
        query = update.callback_query
        user_id = query.from_user.id
        data = context.user_data.get(user_id)

        if not data or data['current_question'] >= 10:
            return

        selected_answer = int(query.data)
        question = data['quiz_questions'][data['current_question']]
        
        # Проверка правильного ответа
        if selected_answer == question['correct']:
            data['score'] += 1

        # Переход к следующему вопросу
        data['current_question'] += 1
        query.answer()

        if data['current_question'] < 10:
            self._send_question(update, context)
        else:
            self._finish_quiz(update, context)

    def _finish_quiz(self, update, context):
        """Завершение теста и вывод результатов"""
        user_id = update.effective_user.id
        data = context.user_data[user_id]
        score = data['score']
        
        # Определение звания
        rank = "Новичок"
        for key in sorted(self.rank_titles.keys(), reverse=True):
            if score >= key:
                rank = self.rank_titles[key]
                break

        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Тест завершен!\n\n"
                 f"Правильных ответов: {score}/10\n"
                 f"Ваше звание: {rank}"
        )
        del context.user_data[user_id]

# Пример использования в основном скрипте бота:
# from test import QuizHandler
# quiz_handler = QuizHandler()
# 
# Добавьте обработчики:
# application.add_handler(CommandHandler('start_quiz', quiz_handler.start_quiz))
# application.add_handler(CallbackQueryHandler(quiz_handler.handle_answer))