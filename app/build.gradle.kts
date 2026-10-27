plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "dev.tvlivesubs"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.tvlivesubs"
        minSdk = 29 // AudioPlaybackCapture
        targetSdk = 30 // matches the Mi Box (Android 11)
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        abortOnError = false
    }
}

dependencies {
    implementation(libs.kotlinx.coroutines.android)
}
