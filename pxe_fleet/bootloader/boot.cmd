# SD loader: all changes below affect RAM only. Firmware slots update from Linux.
setenv autoload no
setenv netretry no
setenv bootp_retry_period 20000
setenv tftptimeout 3000
setenv tftptimeoutcountmax 5
setenv tftpblocksize 1468
setenv fleet_server @SERVER@
setenv kernel_addr_r @KERNEL_ADDR@
setenv ramdisk_addr_r @INITRD_ADDR@
setenv fdt_addr_r @FDT_ADDR@
setenv fleet_env_addr 0x05500000
setenv fleet_card_id
setenv fleet_part 1
setenv fleet_tryboot 0
if fdt addr ${fdt_addr}; then
    fdt get value fleet_part /chosen/bootloader partition
    fdt get value fleet_tryboot /chosen/bootloader tryboot
fi
if fatload mmc 0:1 ${fleet_env_addr} card.env; then
    env import -d -t ${fleet_env_addr} ${filesize} fleet_card_id
fi
# Keep the firmware-provided board description, MAC, memory and overlays.
# Boot files on SD do not contain a Linux kernel or initramfs.
while true; do
    setenv fleet_ready no
    setexpr fleet_board_id ${serial#} \& ffffffff
    if itest.l ${fleet_board_id} == @SERIAL@; then
        usb start
        if dhcp; then
            setenv serverip ${fleet_server}
            if tftpboot ${fleet_env_addr} @SERIAL@/boot.env; then
                if itest.l ${filesize} -gt 0 && itest.l ${filesize} -le 0x10000; then
                    # -d clears missing variables. Import only protocol fields;
                    # the server cannot replace local commands/load addresses.
                    if env import -d -t ${fleet_env_addr} ${filesize} @FIELDS@; then
                        if test "${fleet_format}" = "1" && test "${fleet_model}" = "@MODEL@" && test "${fleet_serial}" = "@SERIAL@"; then
                            if itest.l ${fleet_kernel_size} -gt 0 && itest.l ${fleet_kernel_size} -le @KERNEL_LIMIT@ && itest.l ${fleet_initrd_size} -gt 0 && itest.l ${fleet_initrd_size} -le @INITRD_LIMIT@; then
                                if fdt move ${fdt_addr} ${fdt_addr_r} 0x100000; then
                                    if tftpboot ${kernel_addr_r} ${fleet_kernel}; then
                                        if itest.l ${filesize} == ${fleet_kernel_size}; then
                                            hash sha256 ${kernel_addr_r} ${filesize} fleet_actual_hash
                                            if test "${fleet_actual_hash}" = "${fleet_kernel_sha256}"; then
                                                if tftpboot ${ramdisk_addr_r} ${fleet_initrd}; then
                                                    if itest.l ${filesize} == ${fleet_initrd_size}; then
                                                        hash sha256 ${ramdisk_addr_r} ${filesize} fleet_actual_hash
                                                        if test "${fleet_actual_hash}" = "${fleet_initrd_sha256}"; then
                                                            setenv fleet_ready yes
                                                        fi
                                                    fi
                                                fi
                                            fi
                                        fi
                                    fi
                                fi
                            fi
                        fi
                    fi
                fi
            fi
        fi
    else
        echo "PXE Fleet: SD card belongs to a different Pi (@SERIAL@)"
    fi
    if test "${fleet_ready}" = "yes"; then
        echo "PXE Fleet: booting generation ${fleet_generation}"
        setenv bootargs ${fleet_args} fleet.sd=2 fleet.card=${fleet_card_id} fleet.slot=${fleet_part} fleet.sd_model=@BOOT_MODEL@
        @BOOT@ ${kernel_addr_r} ${ramdisk_addr_r}:${fleet_initrd_size} ${fdt_addr_r}
    fi
    echo "PXE Fleet: boot unavailable or invalid; retrying in 10 seconds"
    usb stop
    sleep 10
done
