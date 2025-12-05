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
import time
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
if "session_started" not in st.session_state:
    st.session_state.session_started = False
if "last_hint" not in st.session_state:
    st.session_state.last_hint = None

# ==========================================
# 2. MODEL SELECTOR
# ==========================================
def get_model(system_instruction_text=None):
    """Returns a configured model with the Knowledge Base as System Instruction."""
    try:
        # Priority: Flash 1.5 (High Rate Limits) -> Flash 8b -> Pro
        model_name = "models/gemini-1.5-flash"
        
        # Configure the model with the Books as the 'System Instruction'
        # This prevents the 'No Audio' error by keeping context separate from prompts
        if system_instruction_text:
            return genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_instruction_text
            )
        else:
            return genai.GenerativeModel(model_name)
    except Exception as e:
        st.error(f"Model Error: {e}")
        return None

# ==========================================
# 3. GOOGLE DRIVE HELPER FUNCTIONS
# ==========================================
@st.cache_resource
def get_drive_service():
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
# 4. AUDIO & LOGIC HELPERS
# ==========================================
folder_id = st.secrets["drive"]["folder_id"]
with st.spinner("Loading Training Materials..."):
    kb_text, file_names = load_knowledge_base_from_drive(folder_id)

async def text_to_speech(text, voice):
    try:
        communicate = edge_tts.Communicate(text, voice)
        mp3_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data += chunk["data"]
        return mp3_data
    except Exception:
        return None

def play_audio_autoplay(audio_bytes):
    if audio_bytes:
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
    agent_name = st.text_input("Agent Name", placeholder="Enter your name")
    
    if file_names:
        st.info(f"📚 {len(file_names)} Files Loaded")
    
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
        st.session_state.session_started = False
        st.session_state.last_hint = None
        st.rerun()

# ==========================================
# 6. MODE 1: ROLEPLAY AS REALTOR
# ==========================================
if mode == "Roleplay as Realtor":
    st.title("🏡 Roleplay as Realtor")
    st.markdown("You are the **Realtor**. The AI is a **Skeptical Buyer**.")
    
    if not agent_name:
        st.warning("Enter Agent Name in sidebar.")
        st.stop()
        
    if not kb_text:
        st.error("Knowledge Base Empty.")

    progress = st.session_state.turn_count / 10
    st.progress(progress, text=f"Turn {st.session_state.turn_count}/10")

    # Limit context to avoid 429 Errors (Quota Exceeded)
    # We use this as the "System Instruction" (The Brain's Background)
    context_safe = kb_text[:500000] # 500k characters is safe for 1.5 Flash
    
    system_persona = f"""
    You are a SKEPTICAL HOME BUYER. The user is a Realtor/ISA.
    
    CONTEXT / KNOWLEDGE BASE:
    {context_safe}
    
    BEHAVIOR:
    1. You are roleplaying. Keep responses concise (1-2 sentences).
    2. Use the questions/objections found in the CONTEXT.
    3. At the start, pick ONE random objection from the CONTEXT to open the call.
    4. Always output JSON.
    """

    # --- STEP 1: START BUTTON (AI SPEAKS FIRST) ---
    if not st.session_state.session_started:
        if st.button("🚀 Start Roleplay (Buyer Speaks First)", type="primary"):
            with st.spinner("Buyer is selecting a random objection..."):
                try:
                    # Initialize Model with System Instruction (Context)
                    model = get_model(system_persona)
                    
                    # Simple prompt to trigger the first turn
                    # We pass it as a list to be safe
                    init_prompt = ["Start the conversation. Pick one random objection from the context and say it to the agent."]
                    
                    response = model.generate_content(
                        init_prompt, 
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    resp_json = json.loads(response.text)
                    # Handle cases where AI puts the text in different JSON fields
                    opening_line = resp_json.get("response_text", resp_json.get("text", "Hello."))
                    
                    # Save to history
                    st.session_state.chat_history.append({"role": "Buyer", "content": opening_line})
                    st.session_state.session_started = True
                    st.session_state.turn_count = 1
                    
                    # Generate Audio
                    tts_audio = asyncio.run(text_to_speech(opening_line, voice_option))
                    play_audio_autoplay(tts_audio)
                    st.rerun()
                    
                except Exception as e:
                    st.error(f"Error starting session: {e}")
                    if "429" in str(e):
                        st.warning("⏳ Quota Exceeded. Please wait 1 minute and try again.")

    # --- STEP 2: MAIN LOOP ---
    else:
        # Show History
        for msg in st.session_state.chat_history:
            if msg["role"] == "Buyer":
                st.info(f"**Buyer:** {msg['content']}")
            else:
                st.write(f"**You:** {msg['content']}")

        # Hint System
        if st.button("💡 Need a Hint?"):
            with st.spinner("Coach is thinking..."):
                model = get_model(system_persona)
                history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                hint_req = [f"The conversation history is:\n{history_context}\n\nGive the Agent 1 short tip on how to respond to the last objection."]
                hint_resp = model.generate_content(hint_req)
                st.session_state.last_hint = hint_resp.text
        
        if st.session_state.last_hint:
            st.warning(f"**Coach Whisper:** {st.session_state.last_hint}")

        # Audio Input
        audio_input = st.audio_input("Record your response")

        if audio_input and st.session_state.roleplay_active:
             if st.session_state.turn_count < 10:
                with st.spinner("The Buyer is thinking..."):
                    
                    # 1. AUTO-DETECT FORMAT
                    audio_input.seek(0)
                    audio_bytes = audio_input.read()
                    
                    if len(audio_bytes) < 100:
                        st.error("No audio captured.")
                        st.stop()
                    
                    if audio_bytes[:4].startswith(b'RIFF'):
                        mime_type = "audio/wav"
                    else:
                        mime_type = "audio/webm"
                    
                    # 2. SEND TO GEMINI
                    # We create a FRESH model instance to ensure clean state
                    model = get_model(system_persona)
                    
                    history_context = "\n".join([f"{x['role']}: {x['content']}" for x in st.session_state.chat_history])
                    
                    # We pass the Audio + The History Instructions
                    user_turn_prompt = f"""
                    HISTORY SO FAR:
                    {history_context}
                    
                    INSTRUCTIONS:
                    1. Listen to the Agent's audio response.
                    2. Respond naturally as the Buyer (Keep it short).
                    3. Output JSON:
                    {{
                        "response_text": "Spoken response",
                        "coach_hints": ["Hint 1", "Hint 2"],
                        "suggested_response": "The PERFECT script the agent should have used."
                    }}
                    """
                    
                    try:
                        response = model.generate_content(
                            [user_turn_prompt, {"mime_type": mime_type, "data": audio_bytes}],
                            generation_config={"response_mime_type": "application/json"}
                        )
                        
                        response_json = json.loads(response.text)
                        ai_text = response_json.get("response_text", "")
                        hints = response_json.get("coach_hints", [])
                        better_response = response_json.get("suggested_response", "No suggestion.")
                        
                        tts_audio = asyncio.run(text_to_speech(ai_text, voice_option))
                        
                        st.session_state.chat_history.append({"role": "Agent", "content": "(Audio Input)"})
                        st.session_state.chat_history.append({"role": "Buyer", "content": ai_text})
                        st.session_state.turn_count += 1
                        st.session_state.last_hint = None
                        
                        play_audio_autoplay(tts_audio)
                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"AI Error: {e}")
                        if "429" in str(e):
                             st.error("Too many requests (Quota). Please wait 60 seconds.")

    # End Game
    if st.session_state.turn_count >= 10:
        st.divider()
        st.header("🏁 Session Over!")
        if st.button("Save Scorecard"):
             save_scorecard(agent_name, 10, "Session Complete")

# ==========================================
# 7. MODE 2: ROLEPLAY AS HOMEBUYER
# ==========================================
elif mode == "Roleplay as Homebuyer":
    st.title("🎓 Roleplay as Homebuyer")
    st.markdown("You act as the **Buyer**. Throw objections!")
    
    # Mode 2 also needs the System Instruction setup
    context_safe_mc = kb_text[:500000]
    system_persona_mc = f"""
    You are the PERFECT REALTOR.
    CONTEXT: {context_safe_mc}
    Output JSON: {{ "rebuttal_text": "...", "why_it_works": "..." }}
    """
    
    audio_input_mc = st.audio_input("State your objection")
    
    if audio_input_mc and kb_text:
        with st.spinner("Formulating rebuttal..."):
            audio_input_mc.seek(0)
            audio_bytes_mc = audio_input_mc.read()
            
            if audio_bytes_mc[:4].startswith(b'RIFF'):
                mime_type_mc = "audio/wav"
            else:
                mime_type_mc = "audio/webm"

            try:
                model = get_model(system_persona_mc)
                
                response_mc = model.generate_content(
                    ["Handle this objection perfectly:", {"mime_type": mime_type_mc, "data": audio_bytes_mc}],
                    generation_config={"response_mime_type": "application/json"}
                )
                resp_json_mc = json.loads(response_mc.text)
                rebuttal = resp_json_mc["rebuttal_text"]
                explanation = resp_json_mc["why_it_works"]
                
                tts_audio_mc = asyncio.run(text_to_speech(rebuttal, voice_option))
                
                st.success(f"**Agent Rebuttal:** {rebuttal}")
                play_audio_autoplay(tts_audio_mc)
                with st.expander("Why this works"):
                    st.write(explanation)
            except Exception as e:
                st.error(f"Error: {e}")
