import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: page
    required property var controller
    property var theme: ApplicationWindow.window.theme
    spacing: 18

    Panel {
        Layout.fillWidth: true
        implicitHeight: deviceContent.implicitHeight + 44
        ColumnLayout {
            id: deviceContent
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: 22; spacing: 14
            Text { text: "连接你的 iPhone"; font.pixelSize: 21; font.weight: Font.DemiBold; color: page.theme.text }
            Text { Layout.fillWidth: true; text: "自动检测 USB 连接 · iOS 17+ 使用独立开发者通道，首次准备镜像可能需要联网"; wrapMode: Text.Wrap; color: page.theme.muted; font.pixelSize: 12 }
            RowLayout {
                Layout.fillWidth: true; spacing: 10
                ZCombo { id: device; objectName: "iosDevice"; Layout.fillWidth: true; model: page.controller.devices; textRole: "name"; currentIndex: page.controller.state.selectedIndex; onActivated: page.controller.selectDevice(currentIndex); enabled: !page.controller.state.busy; displayText: currentIndex >= 0 ? currentText : "等待设备 / 请选择设备" }
                ZButton { text: "检测设备"; enabled: !page.controller.state.busy && !page.controller.state.driverInstalling; onClicked: page.controller.scan() }
            }
            Text { Layout.fillWidth: true; textFormat: Text.PlainText; text: page.controller.state.status + " · " + page.controller.state.message; wrapMode: Text.Wrap; color: page.theme.accent; font.pixelSize: 12 }
        }
    }

    Panel {
        Layout.fillWidth: true
        implicitHeight: locationContent.implicitHeight + 44
        ColumnLayout {
            id: locationContent
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: 22; spacing: 14
            RowLayout {
                Layout.fillWidth: true
                Text { text: "选择位置"; color: page.theme.text; font.pixelSize: 15; font.weight: Font.DemiBold }
                Item { Layout.fillWidth: true }
                ZButton { text: "重读地点"; secondary: true; enabled: !page.controller.state.busy; onClicked: page.controller.reloadLocations() }
            }
            ZCombo {
                id: preset; objectName: "iosPreset"
                Layout.fillWidth: true; model: page.controller.locations; textRole: "name"; currentIndex: -1
                displayText: currentIndex < 0 ? "从 location.md 选择预设地点" : currentText
                enabled: !page.controller.state.busy
                onActivated: {
                    longitude.text = page.controller.locations[currentIndex].longitude
                    latitude.text = page.controller.locations[currentIndex].latitude
                    coordinateSystem.currentIndex = 1
                }
            }
            RowLayout {
                Layout.fillWidth: true; spacing: 14
                ColumnLayout {
                    Layout.fillWidth: true
                    Text { text: "经度"; color: page.theme.muted; font.pixelSize: 11 }
                    ZField { id: longitude; objectName: "iosLongitude"; Layout.fillWidth: true; placeholderText: "-180 ～ 180"; enabled: !page.controller.state.busy; inputMethodHints: Qt.ImhFormattedNumbersOnly }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Text { text: "纬度"; color: page.theme.muted; font.pixelSize: 11 }
                    ZField { id: latitude; objectName: "iosLatitude"; Layout.fillWidth: true; placeholderText: "-90 ～ 90"; enabled: !page.controller.state.busy; inputMethodHints: Qt.ImhFormattedNumbersOnly }
                }
            }
            ZCombo { id: coordinateSystem; objectName: "iosCoordinateSystem"; Layout.fillWidth: true; model: ["WGS84 · GPS 原始坐标", "GCJ-02 · 高德 / 腾讯地图"]; currentIndex: 1; enabled: !page.controller.state.busy }
            Text { Layout.fillWidth: true; text: "预设地点默认按高德坐标转换为 WGS84 后发送。也可手动输入经纬度并选择对应坐标系。"; wrapMode: Text.Wrap; color: page.theme.muted; font.pixelSize: 11 }
            RowLayout {
                Layout.fillWidth: true; spacing: 10
                ZButton { text: "应用到设备"; objectName: "iosApply"; enabled: !page.controller.state.busy && !page.controller.state.closing && !page.controller.state.driverInstalling && device.currentIndex >= 0 && longitude.text.trim().length > 0 && latitude.text.trim().length > 0
                    onClicked: page.controller.applyLocation(device.currentIndex, longitude.text, latitude.text, coordinateSystem.currentIndex === 0 ? "wgs84" : "gcj02") }
                ZButton { text: "恢复真实定位"; secondary: true; enabled: page.controller.state.active && !page.controller.state.busy; onClicked: page.controller.resetLocation() }
            }
            Text { Layout.fillWidth: true; text: "关闭窗口后保持后台运行。退出程序时会尝试恢复真实定位，请保留 USB 连接。"; wrapMode: Text.Wrap; color: page.theme.muted; font.pixelSize: 11 }
        }
    }

    Panel {
        Layout.fillWidth: true
        implicitHeight: driverContent.implicitHeight + 44
        ColumnLayout {
            id: driverContent
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
            anchors.margins: 22; spacing: 14
            Text { text: "Apple USB 组件"; color: page.theme.text; font.pixelSize: 15; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; text: "首次使用可安装驱动与通信组件。将从 Apple 和 Microsoft 下载，并请求 Windows 管理员授权；已有组件会保留。"; wrapMode: Text.Wrap; color: page.theme.muted; font.pixelSize: 12 }
            ZButton { text: page.controller.state.driverInstalling ? "正在准备驱动…" : "安装 Apple USB 驱动"; secondary: true; enabled: !page.controller.state.busy && !page.controller.state.driverInstalling; onClicked: page.controller.installDrivers() }
        }
    }
}
