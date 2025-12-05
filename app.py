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
st.set_page_config(page_title="Sales Dojo", layout="wide", page_icon="🥋")

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
if "roleplay_active" not in st.session_state:
    st.session_state.roleplay_active = True

# ==========================================
# 2. DEBUG: CHECK AVAILABLE MODELS
# ==========================================
def get_valid_model_name():
    """Checks which models are available to this API Key."""
    try:
        # We manually list the most capable models in order of preference
        # We prioritize Flash because it is fast for audio
        candidates = [
            "models/gemini-1.5-flash",
            "models/gemini-1.5-flash-001",
            "models/gemini-1.5-pro",
            "models/gemini-2.0-flash-exp" # Experimental newest model
        ]
        
        # Get actual available list from API
        my_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        
        # Find the first match
        for cand in candidates:
            if cand in my_models:
                return cand
        
        # Fallback
        if my_models:
            return my_models[0]
            
        return None
    except Exception as e:
        return None

# Determine the best model dynamically
active_model_name = get_valid_model_name()

# ==========================================
# 3. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================

@st.cache_resource
def get_drive_service():
    """Authenticates with Google Drive using the Service Account in secrets."""
    try:
        service_account_info = st.secrets["connections"]["gsheets"]
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Failed to connect to Google Drive: {e}")
        return None

@st.cache_data(ttl=3600) 
def load_knowledge_base_from_drive(folder_id):
    """Downloads all PDFs from the specific Drive folder and extracts text."""
    service = get_drive_service()
    if not service:
        return "", []
    
    full_text = ""
    file_list_summary = []

    try:
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
            fields="files(id, name)"
        ).execute()
        items = results.get('files', [])

        if not items:
            return "", []

        for item in items:
            request = service.files().get_media(fileId=item['id'])
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            
            done = False
            while done is False:
                status, done = downloader.next_chunk()

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
# 4. APP LOGIC
# ==========================================

# LOAD KB FROM DRIVE
folder_id = st.secrets["drive"]["folder_id"]
with st.spinner("Loading Training Materials..."):
    kb_text, file_names = load_knowledge_base_from_drive(folder_id)

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
# 5. SIDEBAR
# ==========================================
with st.sidebar:
    st.title("🥋 Dojo Settings")
    
    # DEBUG INFO
    if active_model_name:
        st.success(f"🟢 Connected to Brain: {active_model_name}")
    else:
        st.error("🔴 API Key Invalid or No Models Found")
        st.info("Check your API Key in Google AI Studio.")
    
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    st.divider()
    st.subheader("📚 Knowledge Base")
    if file_names:
        st.success(f"Loaded {len(file_names)} Files")
        with st.expander("View Files"):
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
        "AI Voice",
        ["en-US-ChristopherNeural", "en-US-JennyNeural", "en-US-GuyNeural", "en-US-AriaNeural"],
        index=0
    )
    
    mode = st.radio("Select Training Mode", ["Roleplay as Realtor", "Roleplay as Homebuyer"])
    
    if st.button("Reset Session"):
        st.session_state.chat_history = []
        st.session_state.turn_count = 0
        st.session_state.roleplay_active = True
        st.rerun()

# ==========================================
# 6. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**. Keep the deal alive!")
    
    if not agent_name:
        st.warning("Please enter your Agent Name in the sidebar to begin.")
        st.stop()

    if not active_model_name:
        st.error("⚠️ AI Brain not connected. Check Sidebar for errors.")
        st.stop()

    if not kb_text:
        st.error("⚠️ Knowledge Base is empty. Add PDFs to Drive.")

    # Progress Bar
    progress = st.session_state.turn_count / 10
    st.progress(progress, text=f"Turn {st.session_state.turn_count}/10")

    audio_input = st.audio_input("Record your pitch/response")

    if audio_input and st.session_state.roleplay_active:
        if st.session_state.turn_count < 10:
            with st.spinner("The Buyer is thinking..."):
                
                # CRITICAL FIX: REWIND AUDIO BUFFER
                audio_input.seek(0)
                audio_bytes = audio_input.read()
                
                # Check for empty audio
                if len(audio_bytes) < 100:
                    st.error("⚠️ Audio recording failed (File too small). Please try recording again.")
                    st.stop()

                # USE DYNAMICALLY FOUND MODEL
                model = genai.GenerativeModel(active_model_name)
                
                context_safe = kb_text[:900000] 
                
                system_prompt = f"""
                You are a SKEPTICAL HOME BUYER. The user is a Realtor/ISA.
                
                CONTEXT FROM TRAINING MATERIALS (Books & Questions List):
                {context_safe} 
                
                INSTRUCTIONS:
                1. The context includes a list of 120+ questions/objections.
                2. Silently select 10 RANDOM objections from that list to use for this session.
                3. Respond naturally as the buyer using one of those objections if it fits the flow, or make up a relevant one.
                4. CRITICAL: In the 'suggested_response' field, write exactly what the Realtor SHOULD have said to handle your last objection perfectly.
                
                OUTPUT JSON:
                {{
                    "response_text": "Your spoken response as the buyer",
                    "coach_hints": ["Hint 1 on tone", "Hint 2 on strategy"],
                    "suggested_response": "The perfect script/rebuttal the agent SHOULD have used."
                }}
                """
                
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                full_prompt = f"{system_prompt}\n\nHISTORY:\n{history_context}\n\nRespond to the audio input."

                try:
                    response = model.generate_content
