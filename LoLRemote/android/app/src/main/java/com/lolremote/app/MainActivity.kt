package com.lolremote.app

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions

/**
 * PC의 LoL 리모컨 서버 화면을 보여주는 앱.
 * 처음 한 번 PC의 QR을 스캔하면 주소(토큰 포함)를 저장하고, 다음부터는 바로 접속합니다.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private val prefs by lazy { getSharedPreferences("lol_remote", Context.MODE_PRIVATE) }
    private val scanner = registerForActivityResult(ScanContract()) { handleScan(it.contents) }

    // 같은 네트워크(사설 IP)와 Tailscale(100.64.0.0/10) 주소만 허용
    private val privateHost = Regex(
        """^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|127\.).*"""
    )

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#0A1428"))
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true // 룬 프리셋(localStorage) 저장에 필요
        web.addJavascriptInterface(Bridge(), "LolBridge")
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView, request: WebResourceRequest, error: WebResourceError
            ) {
                if (request.isForMainFrame && request.url.scheme != "file") showOffline()
            }
        }
        setContentView(web)

        if (savedUrl() == null) scan() else load()
    }

    private fun savedUrl(): String? = prefs.getString("url", null)

    private fun load() {
        val url = savedUrl()
        if (url == null) showOffline() else web.loadUrl(url)
    }

    private fun showOffline() = web.loadUrl("file:///android_asset/offline.html")

    private fun scan() {
        scanner.launch(
            ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("PC에 뜬 QR 코드를 스캔하세요")
                .setBeepEnabled(false)
                .setOrientationLocked(false)
        )
    }

    private fun handleScan(text: String?) {
        if (text == null) { // 스캔 취소
            if (savedUrl() == null) showOffline()
            return
        }
        val uri = Uri.parse(text)
        val valid = (uri.scheme == "http" || uri.scheme == "https") &&
            !uri.getQueryParameter("token").isNullOrEmpty() &&
            privateHost.matches(uri.host ?: "")
        if (!valid) {
            Toast.makeText(this, "LoL 리모컨 QR이 아니에요.", Toast.LENGTH_LONG).show()
            if (savedUrl() == null) showOffline()
            return
        }
        prefs.edit().putString("url", text).apply()
        web.loadUrl(text)
    }

    /** 웹 화면(index.html, offline.html)에서 호출하는 기능 */
    inner class Bridge {
        @JavascriptInterface
        fun rescan() = runOnUiThread { scan() }

        @JavascriptInterface
        fun retry() = runOnUiThread { load() }

        @JavascriptInterface
        fun isPaired(): Boolean = savedUrl() != null
    }
}
