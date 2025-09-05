# --- HiQuery Backend ---
# File: app.py

import os
import re # Import the regular expression module
import firebase_admin
from firebase_admin import credentials, firestore, auth
import google.generativeai as genai
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import json
from datetime import datetime, timedelta

# --- INITIALIZATION ---

app = Flask(__name__)
app.secret_key = 'your-very-secret-and-random-key-for-hiquery'

# Initialize Firebase
cred = credentials.Certificate("serviceAccountKey.json")
try:
    firebase_admin.initialize_app(cred)
    print("Firebase App Initialized successfully!")
except ValueError:
    print("Firebase App already initialized.")
db = firestore.client()

# Configure Gemini API
try:
    # IMPORTANT: Replace "YOUR_GEMINI_API_KEY" with your actual key
    genai.configure(api_key="AIzaSyAKaJkbCjubcyJSQ9NnC75ADd-p7CYDGR8")
    model = genai.GenerativeModel('gemini-1.5-flash-latest')
    print("Gemini API Configured successfully!")
except Exception as e:
    print(f"Error configuring Gemini API: {e}")
    model = None

# --- CONSTANTS ---
ADMIN_SECRET_CODE = "SuperAdmin123"
STRUGGLE_SCORE_THRESHOLD = 60 # Percentage score below which a quiz is considered a struggle

# --- HELPER TO CLEAN MARKDOWN FROM AI RESPONSES ---
def clean_ai_text(text):
    """Removes common markdown formatting from text."""
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text) # Bold
    text = re.sub(r'\*(.*?)\*', r'\1', text)   # Italic
    text = re.sub(r'#+\s', '', text)            # Headings
    text = re.sub(r'^\s*[\*-]\s', '', text, flags=re.MULTILINE) # List items
    return text.strip()

# --- HELPER: ROBUST AI REQUEST HANDLER ---
def get_gemini_response(prompt, is_json=False):
    if not model:
        raise Exception("AI model is not configured.")
    try:
        response = model.generate_content(prompt)
        if not response.parts:
            raise Exception("The AI returned an empty response.")
        
        raw_text = response.text
        if is_json:
            # Clean the response to ensure it's valid JSON
            clean_text = raw_text.strip().replace("```json", "").replace("```", "")
            return json.loads(clean_text)
        else:
            return clean_ai_text(raw_text)
    except Exception as e:
        raise Exception(f"An error occurred with the AI model: {e}")

# --- MAIN WEBSITE ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        admin_code = request.form.get('admin_code', '')
        user_role = 'admin' if admin_code == ADMIN_SECRET_CODE else 'student'
        try:
            user = auth.create_user(email=email, password=password, display_name=name)
            db.collection('users').document(user.uid).set({
                'name': name, 'email': email, 'role': user_role, 'createdAt': firestore.SERVER_TIMESTAMP
            })
            return redirect(url_for('login'))
        except Exception as e: return f"An Error Occurred during signup: {e}"
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        try:
            user = auth.get_user_by_email(email)
            session['user_id'] = user.uid
            return redirect(url_for('dashboard'))
        except Exception as e: return f"Login Failed: {e}"
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    try:
        user_id = session['user_id']
        user_doc = db.collection('users').document(user_id).get()
        if not user_doc.exists:
            session.pop('user_id', None)
            return "User not found.", 404
        user_data = user_doc.to_dict()

        if user_data.get('role') == 'admin':
            all_students_ref = db.collection('users').where('role', '==', 'student').stream()
            students_data, struggling_students = [], []
            for student in all_students_ref:
                student_dict = {'uid': student.id, **student.to_dict()}
                students_data.append(student_dict)
                quizzes_ref = db.collection('users').document(student.id).collection('quizzes').order_by('takenAt', direction=firestore.Query.DESCENDING).limit(5).stream()
                low_scores, struggle_topics = 0, {}
                for quiz in quizzes_ref:
                    quiz_data = quiz.to_dict()
                    score_percent = (quiz_data.get('score', 0) / 5) * 100
                    if score_percent < STRUGGLE_SCORE_THRESHOLD:
                        low_scores += 1
                        topic = quiz_data.get('topic', 'Unknown Topic')
                        struggle_topics[topic] = struggle_topics.get(topic, 0) + 1
                if low_scores >= 2 and any(count >= 2 for count in struggle_topics.values()):
                    student_dict['struggle_reason'] = f"Struggling with: {max(struggle_topics, key=struggle_topics.get)}"
                    struggling_students.append(student_dict)
            return render_template('dashboard.html', user=user_data, students=students_data, struggling_students=struggling_students)
        else:
            return render_template('dashboard.html', user=user_data)
    except Exception as e: return f"An Error Occurred on Dashboard: {e}"

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('index'))

@app.route('/delete_user/<user_id>', methods=['POST'])
def delete_user(user_id):
    if 'user_id' not in session: return "Not authenticated", 401
    try:
        db.collection('users').document(user_id).delete()
        auth.delete_user(user_id)
        return redirect(url_for('dashboard'))
    except Exception as e: return f"Error deleting user: {e}"

# --- API ROUTES ---
@app.route('/ask', methods=['POST'])
def ask():
    if 'user_id' not in session: return "Authentication required.", 401
    try:
        user_prompt = request.form['prompt']
        db.collection('users').document(session['user_id']).collection('searches').add({'prompt': user_prompt, 'timestamp': firestore.SERVER_TIMESTAMP})
        return get_gemini_response(user_prompt)
    except Exception as e: return str(e), 500

@app.route('/generate_study_plan', methods=['POST'])
def generate_study_plan():
    if 'user_id' not in session: return "Authentication required.", 401
    try:
        topic = request.form.get('topic')
        prompt = f"Create a 7-day study plan for a beginner learning '{topic}'."
        return get_gemini_response(prompt)
    except Exception as e: return str(e), 500

@app.route('/generate_flashcards', methods=['POST'])
def generate_flashcards():
    if 'user_id' not in session: return jsonify({"error": "Authentication required"}), 401
    try:
        topic = request.form.get('topic')
        prompt = f"""Generate a set of 10 flashcards for '{topic}'. Return a strict JSON object with a key "flashcards" (array of objects, each with "front" and "back" keys)."""
        return jsonify(get_gemini_response(prompt, is_json=True))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/generate_quiz', methods=['POST'])
def generate_quiz():
    if 'user_id' not in session: return jsonify({"error": "Authentication required"}), 401
    try:
        topic = request.form.get('topic')
        prompt = f"""Create a 5-question multiple-choice quiz about '{topic}'. Return a strict JSON object with a key "questions" (array of objects, each with "question", "options", and "answer" keys)."""
        return jsonify(get_gemini_response(prompt, is_json=True))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/get_quiz_suggestion', methods=['POST'])
def get_quiz_suggestion():
    if 'user_id' not in session: return jsonify({"error": "Authentication required"}), 401
    try:
        data = request.json
        topic, score, incorrect = data.get('topic'), data.get('score'), data.get('incorrectQuestions', [])
        db.collection('users').document(session['user_id']).collection('quizzes').add({'topic': topic, 'score': score, 'takenAt': firestore.SERVER_TIMESTAMP})
        if not incorrect:
            return jsonify({"suggestion": "Excellent work! You answered all questions correctly."})
        prompt = f"A student scored {score}/5 on a quiz about '{topic}'. They answered these incorrectly: {', '.join(incorrect)}. Provide a brief, encouraging suggestion on what concept they should review."
        return jsonify({"suggestion": get_gemini_response(prompt)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/get_progress_data', methods=['GET'])
def get_progress_data():
    if 'user_id' not in session: return jsonify({"error": "Authentication required"}), 401
    try:
        user_id = session['user_id']
        query = db.collection('users').document(user_id).collection('quizzes').order_by('takenAt', direction=firestore.Query.DESCENDING).limit(100).stream()
        dates = {doc.to_dict()['takenAt'].date() for doc in query}
        streak, today = 0, datetime.now().date()
        if today in dates:
            streak, current_day = 1, today - timedelta(days=1)
            while current_day in dates:
                streak, current_day = streak + 1, current_day - timedelta(days=1)
        query = db.collection('users').document(user_id).collection('quizzes').stream()
        topics = {}
        for doc in query:
            topic = doc.to_dict().get('topic', 'General')
            topics[topic] = topics.get(topic, 0) + 1
        top_topics = dict(sorted(topics.items(), key=lambda item: item[1], reverse=True)[:5])
        return jsonify({"streak": streak, "top_topics": top_topics})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/get_struggle_insight/<student_id>', methods=['POST'])
def get_struggle_insight(student_id):
    if 'user_id' not in session: return "Authentication required", 401
    try:
        quizzes_ref = db.collection('users').document(student_id).collection('quizzes').order_by('takenAt', direction=firestore.Query.DESCENDING).limit(10).stream()
        history = [f"Topic: {q.to_dict().get('topic')}, Score: {q.to_dict().get('score')}/5" for q in quizzes_ref]
        if not history: return "No quiz history to analyze."
        prompt = f"Analyze this quiz history: {'; '.join(history)}. Provide a one-paragraph insight into their struggles and a constructive suggestion for an admin."
        return get_gemini_response(prompt)
    except Exception as e: return str(e), 500

if __name__ == '__main__':
    app.run(debug=True)

