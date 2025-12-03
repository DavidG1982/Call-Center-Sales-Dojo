import streamlit as st
import google.generativeai as genai
from streamlit_gsheets import GSheetsConnection
import edge_tts
import asyncio
import PyPDF2
import pandas as pd
import json
import base64
import io
from datetime import datetime

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Call Center Sales Dojo", layout="wide", page_icon="🥋")

# Check for Secrets
if "GOOGLE_API_KEY" not in st.secrets or "gsheets" not in st.secrets.connections:
    st.error("🚨 Setup Needed: Missing Secrets")
    st.markdown("""
    **Instructions:**
    1. Get a **Gemini API Key** from Google AI Studio.
    2. Set up a **Google Service Account** and share a Google Sheet with the email.
    3. Add these to your `secrets.toml` or Streamlit Cloud Secrets.
    """)
    st.stop()

# Configure Gemini
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])

# Initialize Session State
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "kb_text" not in st.session_state:
    st.session_state.kb_text = ""
if "gauntlet_active" not in st.session_state:
    st.session_state.gauntlet_active = True
if "latest_audio_base64" not in st.session_state:
    st.session_state.latest_audio_base64 = None

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================

def extract_text_from_files(uploaded_files):
    text_content = ""
    for uploaded_file in uploaded_files:
        try:
            if uploaded_file.type == "application/pdf":
                pdf_reader = PyPDF2.PdfReader(uploaded_file)
                for page in pdf_reader.pages:
                    text_content += page.extract_text() + "\n"
            else:
                # Text files
                stringio = io.StringIO(uploaded_file.getvalue().decode("utf-8"))
                text_content += stringio.read() + "\n"
        except Exception as e:
            st.error(f"Error reading {uploaded_file.name}: {e}")
    return text_content

async def text_to_speech(text, voice):
    """Generates MP3 audio bytes using Edge TTS."""
    communicate = edge_tts.Communicate(text, voice)
    mp3_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data += chunk["data"]
    return mp3_data

def play_audio_autoplay(audio_bytes):
    """Embeds audio in HTML to autoplay."""
    b64 = base64.b64encode(audio_bytes).decode()
    md = f"""
        <audio autoplay="true">
        <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        """
    st.markdown(md, unsafe_allow_html=True)

def save_to_gsheets(agent_name, score, feedback):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        # Fetch existing data to append
        existing_data = conn.read()
        
        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": score,
            "Feedback": feedback
        }])
        
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        st.success("✅ Scorecard saved to Google Sheets!")
    except Exception as e:
        st.error(f"Failed to save to Sheets. Is the Sheet shared with the Service Account email? Error: {e}")

# ==========================================
# 3. SIDEBAR UI
# ==========================================
with st.sidebar:
    st.title("🥋 Settings")
    
    agent_name = st.text_input("Agent Name", placeholder="John Doe")
    
    st.subheader("📚 Knowledge Base")
    uploaded_files = st.file_uploader("Upload Training Materials (PDF/TXT)", accept_multiple_files=True)
    
    if uploaded_files:
        if st.button("Process Knowledge Base"):
            with st.spinner("Extracting text..."):
                st.session_state.kb_text = extract_text_from_files(uploaded_files)
                st.success(f"Loaded {len(st.session_state.kb_text)} characters of context.")
    
    st.subheader("🔊 Audio Settings")
    voice_option = st.selectbox(
        "Buyer's Voice",
        ["en-US-ChristopherNeural", "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
        index=0
    )
    
    mode = st.radio("Select Mode", ["The Gauntlet (Evaluation)", "The Masterclass (Training)"])
    
    if st.button("Reset Session"):
        st.session_state.chat_history = []
        st.session_state.turn_count = 0
        st.session_state.gauntlet_active = True
        st.rerun()

# ==========================================
# 4. MODE 1: THE GAUNTLET (EVALUATION)
# ==========================================
if mode == "The Gauntlet (Evaluation)":
    st.title("🔥 The Gauntlet")
    st.markdown("Roleplay as the **Agent**. The AI is a **Skeptical Buyer**. Keep the deal alive for 10 turns!")
    
    if not agent_name:
        st.warning("Please enter your Agent Name in the sidebar to begin.")
        st.stop()

    # Progress Bar
    progress = st.session_state.turn_count / 10
    st.progress(progress, text=f"Turn {st.session_state.turn_count}/10")

    # Audio Input (New in Streamlit 1.39+)
    audio_input = st.audio_input("Record your pitch/response")

    if audio_input and st.session_state.gauntlet_active:
        if st.session_state.turn_count < 10:
            with st.spinner("The Buyer is thinking..."):
                # 1. Convert Audio to Bytes
                audio_bytes = audio_input.read()
                
                # 2. Prepare Gemini Prompt
                model = genai.GenerativeModel("gemini-1.5-flash")
                
                system_prompt = f"""
                You are a roleplaying engine. You are a SKEPTICAL HOME BUYER. 
                The user is a Call Center Agent trying to sell you a service/home.
                
                CONTEXT FROM KNOWLEDGE BASE:
                {st.session_state.kb_text[:50000]} 
                
                INSTRUCTIONS:
                1. Listen to the agent's audio.
                2. Respond naturally as a skeptical buyer. Raise objections based on the KB if applicable.
                3. You must provide JSON output exactly like this:
                {{
                    "response_text": "Your spoken response to the agent...",
                    "coach_hints": ["Hint 1 about tone", "Hint 2 about argument", "Hint 3"]
                }}
                4. Keep response_text concise (under 2 sentences).
                """
                
                # Build History for Context
                # Note: sending raw bytes for the current turn, but previous turns are text summary for context efficiency
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                
                full_prompt = f"{system_prompt}\n\nPREVIOUS CONVERSATION:\n{history_context}\n\nEvaluate the attached audio input."

                try:
                    # 3. Call Gemini
                    response = model.generate_content(
                        [full_prompt, {"mime_type": "audio/wav", "data": audio_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    response_json = json.loads(response.text)
                    ai_text = response_json["response_text"]
                    hints = response_json["coach_hints"]
                    
                    # 4. Generate TTS
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
                    # 5. Update State
                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1
                    
                    # 6. Display Output
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        st.info(f"**Buyer says:** {ai_text}")
                        play_audio_autoplay(tts_audio)
                    with col2:
                        st.markdown("### 🧠 Live Coaching")
                        for hint in hints:
                            st.caption(f"• {hint}")
                            
                except Exception as e:
                    st.error(f"AI Error: {e}")

    # Check for End Game
    if st.session_state.turn_count >= 10 and st.session_state.gauntlet_active:
        st.session_state.gauntlet_active = False
        st.divider()
        st.header("🏁 The Gauntlet is Over!")
        
        with st.spinner("Grading your performance..."):
            model = genai.GenerativeModel("gemini-1.5-flash")
            grading_prompt = f"""
            Review the following sales call history.
            
            HISTORY:
            {st.session_state.chat_history}
            
            OUTPUT JSON:
            {{
                "score": (integer 0-10),
                "feedback_summary": "2-3 sentences summarizing performance."
            }}
            """
            
            grad_resp = model.generate_content(grading_prompt, generation_config={"response_mime_type": "application/json"})
            grad_json = json.loads(grad_resp.text)
            
            final_score = grad_json["score"]
            final_feedback = grad_json["feedback_summary"]
            
            st.metric("Final Score", f"{final_score}/10")
            st.write(final_feedback)
            
            if st.button("Save Result to Scorecard"):
                save_to_gsheets(agent_name, final_score, final_feedback)

# ==========================================
# 5. MODE 2: THE MASTERCLASS (TRAINING)
# ==========================================
elif mode == "The Masterclass (Training)":
    st.title("🎓 The Masterclass")
    st.markdown("Roleplay as the **Buyer**. Throw objections! The AI acts as the **Perfect Agent** using your Knowledge Base.")

    audio_input_mc = st.audio_input("State your objection")
    
    if audio_input_mc:
        with st.spinner("Formulating perfect rebuttal..."):
            audio_bytes_mc = audio_input_mc.read()
            
            model = genai.GenerativeModel("gemini-1.5-flash")
            
            system_prompt_mc = f"""
            You are the PERFECT CALL CENTER AGENT. 
            The user is a Buyer giving you an objection.
            
            USE THIS KNOWLEDGE BASE:
            {st.session_state.kb_text[:50000]}
            
            INSTRUCTIONS:
            1. Handle the objection perfectly based on the text provided.
            2. Output JSON:
            {{
                "rebuttal_text": "What you say to the buyer",
                "why_it_works": "Explanation of the sales technique used."
            }}
            """
            
            try:
                response_mc = model.generate_content(
                    [system_prompt_mc, {"mime_type": "audio/wav", "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )
                
                resp_json_mc = json.loads(response_mc.text)
                rebuttal = resp_json_mc["rebuttal_text"]
                explanation = resp_json_mc["why_it_works"]
                
                # TTS
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice_option))
                
                st.success(f"**Agent Rebuttal:** {rebuttal}")
                play_audio_autoplay(tts_audio_mc)
                
                with st.expander("Why this works (Training Note)", expanded=True):
                    st.write(explanation)
                    
            except Exception as e:
                st.error(f"Error: {e}")
