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
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
st.set_page_config(page_title="Call Center Sales Dojo", layout="wide", page_icon="🥋")

# Check for Secrets
required_secrets = ["GOOGLE_API_KEY", "drive"]
if not all(k in st.secrets for k in required_secrets):
    st.error("🚨 Setup Needed: Missing Google API Key or Drive Folder ID in secrets.")
    st.stop()

# Configure Gemini
genai.configure(api_key=st.secrets["GOOGLE_API_KEY"])

# Initialize Session State
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "turn_count" not in st.session_state:
    st.session_state.turn_count = 0
if "gauntlet_active" not in st.session_state:
    st.session_state.gauntlet_active = True

# ==========================================
# 2. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================

@st.cache_resource
def get_drive_service():
    """Authenticates with Google Drive using the Service Account in secrets."""
    try:
        # We manually construct credentials from the [connections.gsheets] secret
        # because we need Drive Scope, not just Sheets Scope.
        service_account_info = st.secrets["connections"]["gsheets"]
        
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

@st.cache_data(ttl=3600) # Cache for 1 hour so we don't re-download books on every click
def load_knowledge_base_from_drive(folder_id):
    """Downloads all PDFs from the specific Drive folder and extracts text."""
    service = get_drive_service()
    if not service:
        return ""
    
    full_text = ""
    file_list_summary = []

    try:
        # List files in the folder
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            fields="files(id, name)"
        ).execute()
        items = results.get('files', [])

        if not items:
            return "", []

        for item in items:
            # Download file
            request = service.files().get_media(fileId=item['id'])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            
            done = False
            while done is False:
                status, done = downloader.next_chunk()

            # Extract Text using PyPDF2
            file_stream.seek(0)
            pdf_reader = PyPDF2.PdfReader(file_stream)
            file_text = ""
            for page in pdf_reader.pages:
                file_text += page.extract_text() + "\n"
            
            full_text += f"\n\n--- SOURCE: {item['name']} ---\n{file_text}"
            file_list_summary.append(item['name'])
            
    except Exception as e:
        st.error(f"Error reading from Drive: {e}")
        return "", []

    return full_text, file_list_summary

# ==========================================
# 3. APP LOGIC
# ==========================================

# LOAD KB FROM DRIVE
folder_id = st.secrets["drive"]["folder_id"]
with st.spinner("Loading Training Books from Google Drive... (This happens once per hour)"):
    kb_text, file_names = load_knowledge_base_from_drive(folder_id)

# Helper for TTS
async def text_to_speech(text, voice):
    communicate = edge_tts.Communicate(text, voice)
    mp3_data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_data += chunk["data"]
    return mp3_data

def play_audio_autoplay(audio_bytes):
    b64 = base64.b64encode(audio_bytes).decode()
    md = f"""
        <audio autoplay="true">
        <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        """
    st.markdown(md, unsafe_allow_html=True)

def save_scorecard(agent_name, score, feedback):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        existing_data = conn.read(ttl=0)
        new_row = pd.DataFrame([{
            "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Agent Name": agent_name,
            "Score": score,
            "Feedback": feedback
        }])
        updated_df = pd.concat([existing_data, new_row], ignore_index=True)
        conn.update(data=updated_df)
        st.success("✅ Scorecard saved!")
    except Exception as e:
        st.error(f"Failed to save score: {e}")

# ==========================================
# 4. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    st.divider()
    st.subheader("📚 Knowledge Base")
    if file_names:
        st.success(f"Loaded {len(file_names)} Files from Drive")
        with st.expander("View Loaded Files"):
            for f in file_names:
                st.write(f"📄 {f}")
        
        if st.button("🔄 Refresh Drive Files"):
            st.cache_data.clear()
            st.rerun()
    else:
        st.warning("No PDFs found in the connected Drive Folder.")

    st.divider()
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
# 5. MODE 1: THE GAUNTLET
# ==========================================
if mode == "The Gauntlet (Evaluation)":
    st.title("🔥 The Gauntlet")
    st.markdown("Roleplay as the **Agent**. The AI is a **Skeptical Buyer**. Keep the deal alive for 10 turns!")
    
    if not agent_name:
        st.warning("Please enter your Agent Name in the sidebar to begin.")
        st.stop()

    if not kb_text:
        st.error("⚠️ Knowledge Base is empty. Add PDFs to your Google Drive folder.")

    # Progress Bar
    progress = st.session_state.turn_count / 10
    st.progress(progress, text=f"Turn {st.session_state.turn_count}/10")

    audio_input = st.audio_input("Record your pitch/response")

    if audio_input and st.session_state.gauntlet_active:
        if st.session_state.turn_count < 10:
            with st.spinner("The Buyer is thinking..."):
                audio_bytes = audio_input.read()
                model = genai.GenerativeModel("gemini-1.5-flash")
                
                # We limit context to ~1M tokens (Gemini Flash limit). 
                # 2 books is usually fine, but we ensure we don't crash.
                context_safe = kb_text[:800000] 
                
                system_prompt = f"""
                You are a SKEPTICAL HOME BUYER. The user is a Sales Agent.
                
                CONTEXT FROM TRAINING BOOKS:
                {context_safe} 
                
                INSTRUCTIONS:
                1. Listen to the agent.
                2. Respond naturally as a skeptical buyer. Use the book context for specific objections/persona.
                3. Output JSON:
                {{
                    "response_text": "Spoken response (concise)",
                    "coach_hints": ["Hint 1", "Hint 2", "Hint 3"]
                }}
                """
                
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                full_prompt = f"{system_prompt}\n\nHISTORY:\n{history_context}\n\nRespond to the audio."

                try:
                    response = model.generate_content(
                        [full_prompt, {"mime_type": "audio/wav", "data": audio_bytes}],
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    response_json = json.loads(response.text)
                    ai_text = response_json["response_text"]
                    hints = response_json["coach_hints"]
                    
                    tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                    
                    st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                    st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                    st.session_state.turn_count += 1
                    
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

    # End Game
    if st.session_state.turn_count >= 10 and st.session_state.gauntlet_active:
        st.session_state.gauntlet_active = False
        st.divider()
        st.header("🏁 The Gauntlet is Over!")
        
        with st.spinner("Grading your performance..."):
            model = genai.GenerativeModel("gemini-1.5-flash")
            grading_prompt = f"""
            Review this sales call based on the provided Training Material.
            
            TRAINING MATERIAL:
            {kb_text[:200000]}
            
            HISTORY:
            {st.session_state.chat_history}
            
            OUTPUT JSON:
            {{
                "score": (integer 0-10),
                "feedback_summary": "Summary of what they did well vs missed based on the training material."
            }}
            """
            
            grad_resp = model.generate_content(grading_prompt, generation_config={"response_mime_type": "application/json"})
            grad_json = json.loads(grad_resp.text)
            
            final_score = grad_json["score"]
            final_feedback = grad_json["feedback_summary"]
            
            st.metric("Final Score", f"{final_score}/10")
            st.write(final_feedback)
            
            if st.button("Save Result to Scorecard"):
                save_scorecard(agent_name, final_score, final_feedback)

# ==========================================
# 6. MODE 2: THE MASTERCLASS
# ==========================================
elif mode == "The Masterclass (Training)":
    st.title("🎓 The Masterclass")
    st.markdown("Roleplay as the **Buyer**. Throw objections! The AI acts as the **Perfect Agent** using the Books.")
    
    audio_input_mc = st.audio_input("State your objection")
    
    if audio_input_mc and kb_text:
        with st.spinner("Formulating perfect rebuttal..."):
            audio_bytes_mc = audio_input_mc.read()
            model = genai.GenerativeModel("gemini-1.5-flash")
            
            system_prompt_mc = f"""
            You are the PERFECT CALL CENTER AGENT based on the training books provided.
            
            CONTEXT BOOKS:
            {kb_text[:800000]}
            
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
                
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice_option))
                
                st.success(f"**Agent Rebuttal:** {rebuttal}")
                play_audio_autoplay(tts_audio_mc)
                
                with st.expander("Why this works (Training Note)", expanded=True):
                    st.write(explanation)
                    
            except Exception as e:
                st.error(f"Error: {e}")
