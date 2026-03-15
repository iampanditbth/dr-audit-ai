import TelegramBot from 'node-telegram-bot-api'
import express from 'express'
import axios from 'axios'
import dotenv from 'dotenv'
import FormData from 'form-data'
import fs from 'fs'
import path from 'path'
import os from 'os'

dotenv.config()

const token = process.env.TELEGRAM_BOT_TOKEN
const aiServiceUrl = process.env.AI_SERVICE_URL || 'http://localhost:8000'

if (!token) {
  console.error('TELEGRAM_BOT_TOKEN is not set in environment variables.')
  process.exit(1)
}

// Create a bot that uses 'polling' to fetch new updates
const bot = new TelegramBot(token, { polling: true })

// Setup express app (useful if we want to add webhooks or health checks later)
const app = express()
const PORT = process.env.PORT || 3000

app.get('/', (req, res) => {
  res.send('Dr. Audit Telegram Bot is running.')
})

const server = app
  .listen(PORT, () => {
    console.log(`Bot backend running on port ${PORT}`)
  })
  .on('error', (err) => {
    if (err.code === 'EADDRINUSE') {
      console.log(`Port ${PORT} is busy, listening on port 0 instead.`)
      app.listen(0, () => {
        console.log(`Bot backend running on a fallback port`)
      })
    } else {
      console.error('Express server failed:', err)
    }
  })

// A simple in-memory state manager keyed by chat ID
// states: 'IDLE', 'WAITING_ROLE', 'WAITING_JD', 'PROCESSING'
const userState = {}

bot.onText(/\/start/, (msg) => {
  const chatId = msg.chat.id
  userState[chatId] = {
    state: 'IDLE',
    filePath: null,
    jobRole: null,
    jobDescription: null,
  }

  const welcomeMessage = `Hello ${msg.from.first_name}! 👋\n\nI am Dr. Audit, your AI Resume Health Checker.\n\nTo get started, please upload your **PDF Resume** (Max 1MB).`
  bot.sendMessage(chatId, welcomeMessage, { parse_mode: 'Markdown' })
})

bot.on('document', async (msg) => {
  const chatId = msg.chat.id
  const doc = msg.document

  // 1. Validate File Type
  if (doc.mime_type !== 'application/pdf') {
    return bot.sendMessage(
      chatId,
      '❌ Please upload a PDF file. Other formats are not supported.',
    )
  }

  // 2. Validate File Size (1MB = 1048576 bytes)
  if (doc.file_size > 1048576) {
    return bot.sendMessage(
      chatId,
      '❌ The PDF size exceeds 1MB. Please compress it and try again.',
    )
  }

  // Reset state for new upload
  userState[chatId] = {
    state: 'PROCESSING_UPLOAD',
    filePath: null,
    jobRole: null,
    jobDescription: null,
  }

  const statusMsg = await bot.sendMessage(
    chatId,
    '⏳ Downloading and verifying your document...',
  )

  try {
    // Get file path from Telegram
    const fileMatch = await bot.getFile(doc.file_id)
    const fileUrl = `https://api.telegram.org/file/bot${token}/${fileMatch.file_path}`

    // Download file locally to temp directory
    const tempFilePath = path.join(os.tmpdir(), `${chatId}_${Date.now()}.pdf`)
    const response = await axios({
      method: 'GET',
      url: fileUrl,
      responseType: 'stream',
    })

    const writer = fs.createWriteStream(tempFilePath)
    response.data.pipe(writer)

    await new Promise((resolve, reject) => {
      writer.on('finish', resolve)
      writer.on('error', reject)
    })

    // 3. Check if document is a resume via Python AI Service
    bot.editMessageText('🧠 AI is verifying if this document is a resume...', {
      chat_id: chatId,
      message_id: statusMsg.message_id,
    })

    const formData = new FormData()
    formData.append('file', fs.createReadStream(tempFilePath))

    const aiCheckRes = await axios.post(
      `${aiServiceUrl}/check_resume`,
      formData,
      {
        headers: formData.getHeaders(),
      },
    )

    if (!aiCheckRes.data.is_resume) {
      fs.unlinkSync(tempFilePath) // Cleanup
      userState[chatId].state = 'IDLE'
      return bot.editMessageText(
        `❌ Upload rejected.\n\nReason: *${aiCheckRes.data.reason}*\n\nPlease upload a valid professional resume.`,
        {
          chat_id: chatId,
          message_id: statusMsg.message_id,
          parse_mode: 'Markdown',
        },
      )
    }

    // 4. Verification successful, ask for job role
    userState[chatId].state = 'WAITING_ROLE'
    userState[chatId].filePath = tempFilePath
    userState[chatId].statusMsgId = statusMsg.message_id

    bot.editMessageText(
      '✅ Resume verified!\n\nNow, please type the **Job Role** you are applying for (e.g. Senior Software Engineer):',
      {
        chat_id: chatId,
        message_id: statusMsg.message_id,
        parse_mode: 'Markdown',
      },
    )
  } catch (error) {
    console.error('Error processing document:', error)
    bot.sendMessage(
      chatId,
      '❌ An error occurred while processing your document. Please try again.',
    )
    userState[chatId].state = 'IDLE'
  }
})

bot.on('text', async (msg) => {
  const chatId = msg.chat.id
  const text = msg.text

  // Ignore commands
  if (text.startsWith('/')) return

  if (!userState[chatId]) {
    return bot.sendMessage(
      chatId,
      'Please type /start to begin a new analysis.',
    )
  }

  if (userState[chatId].state === 'WAITING_ROLE') {
    userState[chatId].jobRole = text
    userState[chatId].state = 'WAITING_JD'

    return bot.sendMessage(
      chatId,
      `Got it. Role: *${text}*\n\nFinally, please provide key parts of the **Job Description** (e.g. roles & responsibilities) rather than copy-pasting the whole thing:`,
      { parse_mode: 'Markdown' },
    )
  }

  if (userState[chatId].state === 'WAITING_JD') {
    if (!text || text.trim().length === 0) {
      return bot.sendMessage(
        chatId,
        '❌ Job Description cannot be empty. Please provide the details:',
      )
    }

    userState[chatId].jobDescription = text
    userState[chatId].state = 'ANALYZING'

    const statusMsg = await bot.sendMessage(
      chatId,
      '⚡ Dr. Audit is now analyzing your resume against the job description...',
    )

    await performAnalysis(chatId, statusMsg.message_id)
  }
})

async function performAnalysis(chatId, messageId) {
  const state = userState[chatId]
  try {
    const formData = new FormData()
    formData.append('file', fs.createReadStream(state.filePath))
    formData.append('job_role', state.jobRole)
    formData.append('job_description', state.jobDescription)

    const aiResponse = await axios.post(`${aiServiceUrl}/analyze`, formData, {
      headers: formData.getHeaders(),
    })

    const data = aiResponse.data

    // Clean up file
    try {
      fs.unlinkSync(state.filePath)
    } catch (e) {}

    const score = data.ats_score
    let resultMsg = `📊 **Resume Health: ${score}%**\n\n`

    resultMsg += `🎯 **Matched Skills:**\n${(data.matched_skills || []).join(', ') || 'None'}\n\n`
    resultMsg += `⚠️ **Missing Skills:**\n${(data.missing_skills || []).join(', ') || 'None'}\n\n`

    resultMsg += `💼 **Experience Match:** ${data.experience_match || 'N/A'}\n`
    resultMsg += `📍 **Location Match:** ${data.location_match || 'N/A'}\n`
    resultMsg += `🎓 **Education Match:** ${data.education_match || 'N/A'}\n\n`

    if (score < 75 && data.feedback && data.feedback.length > 0) {
      resultMsg += `💡 **Improvement Suggestions:**\n`
      data.feedback.forEach((s) => {
        resultMsg += `- ${s}\n`
      })
    } else if (score >= 75) {
      resultMsg += `✅ Excellent match! Your resume is well-aligned with this role.`
    }

    bot.editMessageText(resultMsg, {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
    })

    // Reset state
    userState[chatId] = {
      state: 'IDLE',
      filePath: null,
      jobRole: null,
      jobDescription: null,
    }
  } catch (error) {
    console.error('Analysis error:', error.response?.data || error.message)

    let errorMessage =
      '❌ An error occurred during AI analysis. Please try again.'
    if (
      error.response?.status === 429 ||
      (error.response?.data?.detail &&
        error.response.data.detail.includes('rate limit'))
    ) {
      errorMessage =
        '⏳ Slow down! The AI service is receiving too many requests. Please wait a minute and try again.'
    }

    bot.editMessageText(errorMessage, {
      chat_id: chatId,
      message_id: messageId,
    })

    try {
      fs.unlinkSync(state.filePath)
    } catch (e) {}
    userState[chatId].state = 'IDLE'
  }
}
